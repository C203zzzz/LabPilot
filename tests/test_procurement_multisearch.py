import pytest

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.deepseek import DeepSeekClient
from backend.infrastructure.search_providers import ProviderConfigurationError
from backend.services.procurement_research import SEARCH_RESULT_LIMIT
from tests.test_reliability_development import research


def test_native_search_only_accepts_server_results_and_preserves_usage():
    def transport(*args):
        return {'content': [
            {'type': 'text', 'text': 'https://invented.example'},
            {'type': 'web_search_tool_result', 'content': [
                {'type': 'web_search_result', 'url': 'https://lab.edu.cn/award', 'title': '采购结果'},
                {'type': 'web_search_result', 'url': 'https://lab.edu.cn/award', 'title': '重复'},
            ]}], 'usage': {'server_tool_use': {'web_search_requests': 2}}}
    client = DeepSeekClient(api_key='test-native', transport=transport)
    assert client.search_web(query='仪器成交') == [{'url': 'https://lab.edu.cn/award', 'title': '重复', 'snippet': ''}]
    assert client.last_usage['server_tool_use']['web_search_requests'] == 2


def test_procurement_uses_two_engines_and_deduplicates_before_fetch(research):
    service, run, fetcher, _ = research
    calls, reads = [], []
    class Provider:
        def __init__(self, name): self.name = name
        def search(self, query):
            calls.append(self.name)
            return [SearchResult('https://lab.edu.cn/award', 'Q10采购成交公告')]
    service.search.providers = [Provider('first'), Provider('second'), fetcher]
    service.fetch_source = lambda run, source: reads.append(source['url']) or {**source, 'body_status': 'fetched', 'body_text': 'Q10采购成交公告', 'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    before = run['usage']['search']
    service.query(run, 'new procurement', 'tender', target={'model':'Q10'})
    assert set(calls) == {'first', 'second'} and len(reads) == 1
    assert run['usage']['search'] == before + 3


def test_a_discovery_call_asks_for_more_than_five_results(research):
    """How many rows a provider is asked for is a request parameter, not a
    budget: one call costs one ``search`` unit whether it returns 5 rows or 10.

    It used to be pinned at 5, and ``exa_mcp`` replied with exactly 5 every
    single time -- the channel sat on its own ceiling, so every candidate URL
    the source was willing to hand over beyond the fifth was never seen. The
    assertion only says "more than 5" so that raising the constant further
    stays a one-line change.
    """
    service, run, _, _ = research
    seen = []

    class Provider:
        name = 'probe'

        def search(self, query):
            seen.append(query.limit)
            return [SearchResult('https://lab.edu.cn/award', 'Q10采购成交公告')]

    service.search.providers = [Provider()]
    service.fetch_source = lambda run, source: {**source, 'body_status': 'fetched',
                                                'body_text': 'Q10采购成交公告',
                                                'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    service.query(run, 'new procurement', 'tender', target={'model': 'Q10'})
    assert seen == [SEARCH_RESULT_LIMIT]
    assert SEARCH_RESULT_LIMIT > 5


def test_a_bot_challenge_quarantines_a_provider_on_its_first_failure(research):
    """A challenge says "you look automated"; the next call two seconds later
    looks the same, so retrying cannot succeed and only spends discovery calls.
    Measured runs burned 6-8 of their 40 calls that way. Timeouts and 5xx are
    genuinely transient and keep their retries, so only the challenge
    short-circuits.
    """
    service, run, _, _ = research
    calls = []

    class Challenged:
        name = 'challenged'

        def search(self, query):
            calls.append(query.text)
            raise ProviderConfigurationError('Challenged returned an access challenge')

    class Flaky:
        name = 'flaky'

        def search(self, query):
            calls.append(query.text)
            raise ProviderConfigurationError('Flaky service unavailable')

    # 全部 provider 都不可用时产品会明确报错，这是预期行为，这里只数调用次数。
    service.search.providers = [Challenged()]
    for _ in range(3):
        try:
            service.query(run, 'new procurement', 'tender', target={'model': 'Q10'})
        except ValueError:
            pass
    assert len(calls) == 1, '被打上反爬标记的 provider 应当第一次就停手'

    calls.clear()
    service.search.providers = [Flaky()]
    for _ in range(3):
        try:
            service.query(run, 'new procurement', 'tender', target={'model': 'Q10'})
        except ValueError:
            pass
    assert len(calls) == 3, '普通故障仍然应当重试到隔离阈值'


def test_primary_domestic_providers_execute_in_parallel(research):
    """多个国内通道并发检索而非串行等待，总耗时接近最慢单个通道且多路结果全部并入。"""
    import time

    service, run, _, _ = research
    calls = []

    class SlowProvider:
        def __init__(self, name, url):
            self.name = name
            self._url = url

        def search(self, _query):
            time.sleep(0.25)
            calls.append(self.name)
            return [SearchResult(self._url, f'Q10 {self.name} 采购成交公告')]

    service.search.providers = [
        SlowProvider('ccgp', 'https://ccgp.gov.cn/award-1'),
        SlowProvider('deepseek_web', 'https://buyer.edu.cn/award-2'),
        SlowProvider('baidu', 'https://zfcg.zj.gov.cn/award-3'),
    ]
    service.fetch_source = lambda r, src: {
        **src, 'body_status': 'fetched', 'body_text': 'Q10 采购成交公告 成交金额 15000 元', 'source_type': 'tender_document',
    }
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 10, 'fetch': 10, 'model': 10}

    t0 = time.perf_counter()
    service.query(run, 'Q10 成交', 'tender', target={'model': 'Q10', 'category': '离心机'})
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.65, f'3 parallel providers sleeping 0.25s each should finish <0.65s, took {elapsed:.3f}s'
    assert set(calls) == {'ccgp', 'deepseek_web', 'baidu'}
    assert len(run['sources']) == 3
    assert all(a['provider_role'] == 'parallel_discovery' for a in run['search_attempts'])


def test_attachment_discovery_and_office_decoders_keep_source_context():
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    from backend.services.research_sources import attachments

    source = {
        'url': 'https://buyer.gov.cn/notices/1',
        'source_type': 'tender_document',
        'body_text': (
            'Q10 采购成交公告\n'
            '[采购合同](../file/download?id=9)\n'
            '[技术明细](https://files.example/item.xlsx)\n'
            '[ unrelated ](https://files.example/other.pdf)'
        ),
    }
    # 三个附件都要被发现、上下文都不能丢。顺序由 ``attachment_rank`` 的分级决定
    # （「技术明细」是关键文件，因此排在前面），分级本身在
    # ``tests/test_attachment_selection.py`` 里钉，这里只钉「都发现了」。
    assert sorted(attachments(source, model='Q10')) == sorted([
        'https://buyer.gov.cn/file/download?id=9',
        'https://files.example/item.xlsx',
        'https://files.example/other.pdf',
    ])

    docx = BytesIO()
    with ZipFile(docx, 'w', ZIP_DEFLATED) as archive:
        archive.writestr(
            'word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Q10 技术明细</w:t></w:r></w:p></w:body></w:document>',
        )
    assert 'Q10 技术明细' in PublicWebSearchProvider.decode_body(docx.getvalue(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    xlsx = BytesIO()
    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.active.append(['型号', '单价(元)'])
    workbook.active.append(['Q10', 1200])
    workbook.save(xlsx)
    workbook.close()
    decoded = PublicWebSearchProvider.decode_body(xlsx.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    assert 'Q10' in decoded and '1200' in decoded


def test_procurement_relevance_rejects_non_instrument_notice_before_fetch(research):
    service, run, _, _ = research

    class Provider:
        name = 'ccgp'

        def search(self, query):
            return [
                SearchResult('https://city.gov.cn/road', '道路改造采购公告', '施工项目'),
                SearchResult('https://lab.edu.cn/award', '大学仪器采购成交公告', '实验室设备'),
            ]

    reads = []
    service.search.providers = [Provider()]
    service.fetch_source = lambda run, source: reads.append(source['url']) or {
        **source, 'body_status': 'fetched', 'body_text': '大学仪器采购成交公告 实验室设备',
        'source_type': 'unknown',
    }
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 8, 'fetch': 8, 'model': 8}
    service.query(run, 'Q10 成交', 'tender', target={'model': 'Q10', 'category': '离心机'})
    assert reads == ['https://lab.edu.cn/award']
    assert any(row['url'] == 'https://city.gov.cn/road' for row in run['skipped_search_results'])


def test_failed_engine_is_recorded_and_falls_back_with_exact_budget(research):
    service, run, fetcher, _ = research
    class Failed:
        name = 'blocked'
        def search(self, query): raise ProviderConfigurationError('challenge')
    class Empty:
        name = 'working'
        def search(self, query): return []
    service.search.providers = [Failed(), Empty()]
    before = run['usage']['search']
    for attempt in range(1, 4):
        service.query(run, f'fresh search {attempt}', 'tender', target={'model':'Q10'})
        assert run['usage']['search'] == before + attempt * 2
        assert [a['status'] for a in run['search_attempts'][-2:]] == ['failed', 'ok']
        if attempt < 3:
            assert 'blocked' not in run.get('unavailable_search_providers', [])
        else:
            assert run['provider_failure_counts']['blocked'] == 3
            assert 'blocked' in run['unavailable_search_providers']


def test_price_notice_after_four_brochures_is_still_examined(research):
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    run['sources'] = [{'url': f'https://vendor.example/{i}', 'body_status': 'fetched',
                      'source_type': 'manufacturer_website', 'body_text': 'Q10技术参数容量10ml'} for i in range(4)]
    run['sources'].append({'url': 'https://lab.example.edu.cn/award', 'body_status': 'fetched',
                          'source_type': 'tender_document', 'body_text': '成交公告\nQ10 数量1台 单价1200元'})
    examined = []
    service.model = lambda run, prompt, data: examined.append(data['url']) or {'prices': []}
    service.prices(run, {'model': 'Q10'})
    assert examined == ['https://lab.example.edu.cn/award']


def test_asset_lead_keeps_missing_currency_and_never_borrows_neighbor_price():
    from backend.services.price_leads import asset_price_leads
    source = {'url': 'https://lab.example.edu.cn/assets.pdf', 'body_status': 'fetched',
              'body_text': '资产编号: 101\n单价: 9000\n型号: AX100\n资产编号: 102\n单价: 15000\n使用方向: 科研\n型号: AX200\n规格: AX200'}
    found = asset_price_leads([source], {'model': 'AX200'})
    assert len(found) == 1 and found[0]['amount'] == 15000 and found[0]['currency'] is None
    assert found[0]['kind'] == 'asset_register' and '9000' not in found[0]['excerpt']
    assert not asset_price_leads([{**source, 'url':'https://dealer.example/assets'}], {'model':'AX200'})
    assert not asset_price_leads([{**source, 'body_text':source['body_text'].replace('单价: 15000', '')}], {'model':'AX200'})


def test_bid_rank_is_not_quantity_and_amount_stays_unverified():
    from backend.services.price_leads import price_leads
    source = {'url':'https://hospital.example/award', 'body_status':'fetched',
              'body_text':'项目 | 型号 | 金额（元） | 排名\n离心机 | AX200 | 518,000.00 | 3'}
    row = price_leads([source], {'model':'AX200'})[0]
    assert row['amount'] == 518000 and row['kind'] == 'bid_amount'
    assert '排名3' in row['note'] and '非已确认成交价' in row['note']
    assert 'unit_price' not in row and 'quantity' not in row


def test_catalogue_amount_stays_a_listing_and_requires_exact_model():
    from backend.services.price_leads import price_leads
    source = {'url':'https://dealer.example/catalogue', 'body_status':'fetched',
              'body_text':'AX200S | 仪器 | ￥9,000\nAX200 | 仪器 | ￥12,000'}
    rows = price_leads([source], {'model':'AX200'})
    assert len(rows) == 1 and rows[0]['amount'] == 12000 and rows[0]['kind'] == 'listing'


def test_unstructured_model_amount_is_retained_as_manual_review_lead():
    from backend.services.price_leads import price_leads
    source = {'url': 'https://lab.example.edu.cn/notice', 'body_status': 'fetched',
              'body_text': '设备型号：AX200\n参考金额 12,000 元，数量和交易状态未说明'}
    rows = price_leads([source], {'model': 'AX200'})
    assert len(rows) == 1
    assert rows[0]['kind'] == 'unparsed_amount'
    assert rows[0]['amount'] == 12000
    assert '数量、交易状态或金额口径未完成结构化' in rows[0]['note']
    assert 'AX200' in rows[0]['excerpt'] and '12,000 元' in rows[0]['excerpt']


def test_body_failure_retry_reuses_successful_search(research):
    service, run, _, _ = research
    calls = []
    class Provider:
        name = 'search'
        def search(self, query):
            calls.append(query.text)
            return [SearchResult('https://lab.edu.cn/retry', 'Q10采购结果')]
    service.search.providers = [Provider()]
    service.fetch_attachments = lambda *args: None
    service.fetch_source = lambda run, source: {**source, 'body_status':'failed', 'source_type':'unknown'}
    service.query(run, 'retry-body-only', 'tender', target={'model':'Q10'})
    before = run['usage']['search']
    service.fetch_source = lambda run, source: {**source, 'body_status':'fetched', 'body_text':'Q10采购成交公告', 'source_type':'unknown'}
    service.query(run, 'retry-body-only', 'tender', target={'model':'Q10'})
    assert calls == ['retry-body-only'] and run['usage']['search'] == before

    assert next(s for s in run['sources'] if s['url'] == 'https://lab.edu.cn/retry')['body_status'] == 'fetched'


def test_parallel_procurement_lists_bind_model_quantity_and_header_unit():
    from backend.services.procurement_tables import price_table_rows
    from backend.services.procurement_rules import validate_price
    body = '中标公告\n货物名称 | 货物品牌 | 货物型号 | 货物数量 | 货物单价(元)\n离心机;培养箱 | 甲;乙 | AX200;BX300 | 1;3 | 149900;50000'
    source = {'url':'https://procure.example.gov.cn/award', 'body_text':body, 'body_status':'fetched', 'source_type':'tender_document'}
    row = price_table_rows(body, 'AX200')[0]
    price = validate_price({**row, 'kind':'award'}, source, {'model':'AX200','brand':'甲'})
    assert price['unit_price'] == 149900 and price['quantity'] == 1
    assert price['table_evidence']['item_index'] == 0 and price['level'] == 'model_reference'
    assert not validate_price({**row, 'kind':'award', 'amount':50000}, source, {'model':'AX200'})
    assert not price_table_rows(body.replace('149900;50000', '199900'), 'AX200')
    assert not price_table_rows(body.replace('货物单价(元)', '标包总价(元)'), 'AX200')


def test_award_table_accepts_quantity_unit_suffix_and_keeps_same_model_price():
    from backend.services.procurement_tables import price_table_rows
    from backend.services.procurement_rules import validate_price

    body = (
        '中标公告\n'
        '主要中标货物的名称 | 规格型号 | 数量 | 单价（元）\n'
        '大容量高速冷冻离心机 | 湘仪VORTEX22K | 1台 | 90,000\n'
    )
    source = {
        'url': 'https://procure.example.gov.cn/vortex22k-award',
        'body_text': body,
        'body_status': 'fetched',
        'source_type': 'tender_document',
    }
    row = price_table_rows(body, 'VORTEX22K')[0]
    result = validate_price({**row, 'kind': 'award'}, source,
                            {'brand': '湘仪', 'model': 'VORTEX22K', 'quantity': None})
    assert row['quantity'] == 1
    assert result['unit_price'] == 90000
    assert result['level'] == 'model_reference'


def test_parallel_procurement_lists_expand_single_common_name_and_brand():
    from backend.services.procurement_tables import price_table_rows

    body = (
        '中标公告\n'
        '货物名称 | 货物品牌 | 规格型号 | 货物数量 | 货物单价(元)\n'
        '生物安全柜 | 知楚 | A2;B2 | 1;2 | 74800;85000'
    )

    first = price_table_rows(body, 'A2')
    second = price_table_rows(body, 'B2')

    assert len(first) == len(second) == 1
    assert first[0]['brand'] == second[0]['brand'] == '知楚'
    assert first[0]['amount'] == 74800 and first[0]['quantity'] == 1
    assert second[0]['amount'] == 85000 and second[0]['quantity'] == 2


def test_parallel_procurement_lists_do_not_expand_price_or_quantity():
    from backend.services.procurement_tables import price_table_rows

    body = (
        '货物名称 | 规格型号 | 货物数量 | 货物单价(元)\n'
        '生物安全柜 | A2;B2 | 1;2 | 74800;85000'
    )

    assert not price_table_rows(body.replace('74800;85000', '74800'), 'A2')
    assert not price_table_rows(body.replace('1;2', '1'), 'A2')


def test_gzip_html_snapshot_is_decoded_before_binary_check():
    import gzip
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    raw = gzip.compress('<html><p>AX200 中标单价1200元</p></html>'.encode())
    assert 'AX200 中标单价1200元' in PublicWebSearchProvider.decode_body(raw, 'text/html')


@pytest.mark.parametrize('separator', [
    '说明：以下为另一张统计表\n',
    '型号 | 数量 | 总价(元)\n',
    '设备名称 | 规格 | 数量 | 合计(元)\n',
])
def test_price_header_does_not_leak_into_another_table(separator):
    from backend.services.procurement_tables import price_table_rows
    from backend.services.price_leads import price_leads
    body = '型号 | 数量 | 单价(元)\nBX300 | 1 | 200\n' + separator + 'AX200 | 3 | 900'
    assert not price_table_rows(body, 'AX200')
    source = {'url': 'https://lab.edu.cn/prices', 'body_status': 'fetched', 'body_text': body}
    assert not price_leads([source], {'model': 'AX200'})


def test_price_projection_cannot_override_the_brand_in_the_source():
    from backend.services.procurement_rules import validate_price
    body = '中标公告\n品牌 | 型号 | 数量 | 单价(元)\n乙 | AX200 | 1 | 900'
    source = {'url': 'https://lab.edu.cn/award', 'source_type': 'tender_document', 'body_status': 'fetched', 'body_text': body}
    raw = {'brand': '甲', 'model': 'AX200', 'amount': 900, 'amount_unit': '元', 'quantity': 1,
           'amount_scope': 'unit', 'kind': 'award', 'excerpt': '乙 | AX200 | 1 | 900'}
    assert not validate_price(raw, source, {'brand': '甲', 'model': 'AX200'})


def test_formal_contract_content_column_can_carry_exact_model_price():
    from backend.services.procurement_tables import price_table_rows
    body = (
        '成交公告\n'
        '设备/服务名称 | 服务内容 | 数量 | 单价(元) | 合计(元)\n'
        '低温培养箱 | SPX-150BIII | 2 | 5800 | 11600\n'
    )
    rows = price_table_rows(body, 'SPX-150BIII')
    assert len(rows) == 1
    assert rows[0]['amount'] == 5800.0
    assert rows[0]['quantity'] == 2.0
    assert rows[0]['amount_scope'] == 'unit'


def test_formal_contract_table_price_is_validated_as_contract_when_no_award_marker():
    from backend.services.procurement_rules import validate_price
    from backend.services.procurement_tables import price_table_rows

    body = (
        '采购合同明细（签订日期：2024-03-18）\n'
        '设备/服务名称 | 服务内容 | 数量 | 单价(元) | 合计(元)\n'
        '低温培养箱 | SPX-150BIII | 2 | 5800 | 11600\n'
    )
    source = {
        'url': 'https://www7.zzu.edu.cn/ztb/info/1136/11076.htm',
        'body_text': body,
        'body_status': 'fetched',
        'source_type': 'tender_document',
    }
    row = price_table_rows(body, 'SPX-150BIII')[0]
    result = validate_price({**row, 'kind': 'contract'}, source,
                            {'brand': '', 'model': 'SPX-150BIII', 'quantity': 2})
    assert result is not None
    assert result['unit_price'] == 5800
    assert result['amount_scope'] == 'unit'
    assert result['kind'] == 'contract'


def test_prices_accepts_formal_contract_table_without_award_marker(research):
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    body = (
        '采购合同明细（签订日期：2024-03-18）\n'
        '设备/服务名称 | 服务内容 | 数量 | 单价(元) | 合计(元)\n'
        '低温培养箱 | SPX-150BIII | 2 | 5800 | 11600\n'
    )
    run['sources'] = [{
        'url': 'https://www7.zzu.edu.cn/ztb/info/1136/11076.htm',
        'body_text': body,
        'body_status': 'fetched',
        'source_type': 'tender_document',
    }]
    prices = service.prices(run, {'brand': '', 'model': 'SPX-150BIII', 'quantity': 2})
    assert len(prices) == 1
    assert prices[0]['kind'] == 'contract'
    assert prices[0]['unit_price'] == 5800
    assert prices[0]['evidence_location']['char_start'] >= 0


def test_changed_failure_reason_does_not_count_as_repeated_site_failure(research):
    service, run, provider, _ = research
    provider.fetch = lambda *args: ''
    run['sources'] = [{'url': 'https://broken.example/', 'body_status': 'failed', 'failure_kind': 'TimeoutError'},
                      {'url': 'https://healthy1.example/', 'body_status': 'fetched'},
                      {'url': 'https://healthy2.example/', 'body_status': 'fetched'}]
    service.recover_sources(run)
    assert run['sources'][0]['body_status'] == 'failed'
    assert run['unresolved_source_errors']


def test_isolated_empty_site_keeps_gap_without_reporting_global_service_failure(research):
    service, run, provider, _ = research
    provider.fetch = lambda *args: ''
    failed = service.fetch_source(run, {'url':'https://empty.example/page'})
    run['sources'] = [failed]
    service.recover_sources(run)
    assert run['unresolved_source_errors']
    run['round'] += 1
    run['sources'] += [{'url':f'https://healthy{i}.example/', 'body_status':'fetched'} for i in range(2)]
    service.recover_sources(run)
    assert not run['unresolved_source_errors']
    assert run['sources'][0]['body_status'] == 'unavailable'
    assert run['sources'][0]['failure_kind'] == 'empty_body' and run['unavailable_sources']


def test_baidu_access_challenge_enters_cooldown_before_quarantine(research):
    from backend.infrastructure.search_providers import ProviderConfigurationError

    service, run, _, _ = research
    baidu_calls = []

    class FlakyBaidu:
        name = 'baidu'

        def search(self, _query):
            baidu_calls.append(1)
            raise ProviderConfigurationError('Baidu returned an access challenge')

    class WorkingCCGP:
        name = 'ccgp'

        def search(self, _query):
            return [SearchResult('https://www.ccgp.gov.cn/notice1', 'Q10 采购成交公告')]

    service.search.providers = [WorkingCCGP(), FlakyBaidu()]
    service.fetch_source = lambda r, src: {
        **src, 'body_status': 'fetched', 'body_text': 'Q10 成交公告 15000 元', 'source_type': 'tender_document',
    }
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 20, 'fetch': 20, 'model': 10}

    # 1st challenge: enters cooldown, NOT quarantined yet
    service.query(run, 'Q10 成交 1', 'tender', target={'model': 'Q10'})
    assert 'baidu' not in (run.get('unavailable_search_providers') or [])
    assert run['provider_challenge_counts']['baidu'] == 1
    assert 'baidu' in run['provider_cooldown_until']

    # Next query during cooldown: skipped with explicit attempt record, no request spent
    service.query(run, 'Q10 成交 2', 'tender', target={'model': 'Q10'})
    assert len(baidu_calls) == 1
    assert [a['status'] for a in run['search_attempts'] if a['provider'] == 'baidu'][-1] == 'cooldown_skipped'

    # Simulate cooldown expiry; 2nd challenge triggers full quarantine
    run['provider_cooldown_until']['baidu'] = 1.0
    service.query(run, 'Q10 成交 3', 'tender', target={'model': 'Q10'})
    assert len(baidu_calls) == 2
    assert 'baidu' in run['unavailable_search_providers']

    # Further query records quarantined_skipped rather than silent skip
    service.query(run, 'Q10 成交 4', 'tender', target={'model': 'Q10'})
    assert [a['status'] for a in run['search_attempts'] if a['provider'] == 'baidu'][-1] == 'quarantined_skipped'


def test_low_value_social_domains_are_filtered_before_fetch(research):
    service, run, _, _ = research
    fetched_urls = []

    class MixedProvider:
        name = 'baidu'

        def search(self, _query):
            return [
                SearchResult('https://www.douyin.com/video/123', 'Q10 中标采购', '单价 12000'),
                SearchResult('https://weibo.com/u/999', 'Q10 中标公告', '成交'),
                SearchResult('https://www.xiaohongshu.com/explore/abc', 'Q10 仪器采购', '报价'),
                SearchResult('https://mp.weixin.qq.com/s/valid-notice', 'Q10 采购中标公告', '高校招标'),
                SearchResult('https://www.ccgp.gov.cn/cggg/1.htm', 'Q10 中标结果公告', '成交'),
            ]

    service.search.providers = [MixedProvider()]
    service.fetch_source = lambda r, src: (
        fetched_urls.append(src['url'])
        or {**src, 'body_status': 'fetched', 'body_text': 'Q10 采购成交公告 成交价 12000 元', 'source_type': 'tender_document'}
    )
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 10, 'fetch': 10, 'model': 10}

    service.query(run, 'Q10 中标', 'tender', target={'model': 'Q10'})
    assert 'https://www.douyin.com/video/123' not in fetched_urls
    assert 'https://weibo.com/u/999' not in fetched_urls
    assert 'https://www.xiaohongshu.com/explore/abc' not in fetched_urls
    assert set(fetched_urls) == {'https://mp.weixin.qq.com/s/valid-notice', 'https://www.ccgp.gov.cn/cggg/1.htm'}
    social_skips = [s for s in run.get('skipped_search_results', []) if '社交媒体' in s.get('reason', '')]
    assert len(social_skips) == 3


def test_default_provider_chain_contains_only_ccgp_baidu_and_optional_deepseek(tmp_path, monkeypatch):
    from backend.main import create_app

    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    app = create_app(database_path=tmp_path / 'chain.sqlite3', storage_dir=tmp_path / 'files')
    try:
        names = [p.name for p in app.state.research_service.search.providers]
        assert names == ['ccgp', 'baidu']
        assert 'bocha' not in names
        assert 'sogou' not in names
    finally:
        app.state.research_service.close()
        app.state.repository.close()


def test_repeated_host_403_skips_further_fetches_and_records_stats(research):
    from urllib.error import HTTPError

    service, run, provider, _ = research
    calls = []

    def fail_403(url, _timeout):
        calls.append(url)
        raise HTTPError(url, 403, 'Forbidden', {}, None)

    provider.fetch = fail_403
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 10, 'fetch': 10, 'model': 10}

    s1 = service.fetch_source(run, {'url': 'https://download.ccgp.gov.cn/a.pdf'})
    s2 = service.fetch_source(run, {'url': 'https://download.ccgp.gov.cn/b.pdf'})
    s3 = service.fetch_source(run, {'url': 'https://download.ccgp.gov.cn/c.pdf'})

    assert len(calls) == 2
    assert s1['failure_kind'] == 'http_403'
    assert s2['failure_kind'] == 'http_403'
    assert s3['body_status'] == 'unavailable'
    assert s3['failure_kind'] == 'http_403'
    assert '跳过重复空耗' in s3['error_reason']
    assert run['fetch_failure_stats']['by_kind']['http_403'] == 3
    assert run['fetch_failure_stats']['by_host']['download.ccgp.gov.cn'] == 3
