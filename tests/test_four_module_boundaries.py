import pytest
from tests.test_reliability_development import research
from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.services.procurement_rules import grounded_parameter, contains_model, price_decision


@pytest.mark.parametrize('models,values,target,value', [
    (['SPX-70BⅢ', 'SPX-150BⅢ', 'SPX-250BⅢ'], [5, 9, 12], 'SPX-150BIII', 9),
    (['ABC-10', 'ABC-20', 'ABC-30'], [3, 7, 11], 'ABC-20', 7),
])
def test_literal_quote_cannot_borrow_another_model_column(models, values, target, value):
    html = '<table><tr><th>型号</th>' + ''.join(f'<th>{m}</th>' for m in models) + '</tr><tr><td>层数</td>' + ''.join(f'<td>{v} 层</td>' for v in values) + '</tr><tr><td>温区</td><td colspan="3">0–65 ℃</td></tr></table>'
    body = PublicWebSearchProvider._clean_html(html)
    source = {'body_text': body, 'body_status': 'fetched', 'source_type': 'manufacturer_website'}
    observed = {'value': value, 'unit': '层', 'excerpt': body}
    assert grounded_parameter(observed, source, target, '层数')
    assert not grounded_parameter({**observed, 'value': values[0]}, source, target, '层数')
    assert contains_model(body, target)
    assert not contains_model('TB' + target, target)


def test_price_decision_preserves_missing_and_not_searched_boundary():
    baseline = {'model': 'A10', 'supplier_price': 6800, 'currency': 'CNY'}
    assert price_decision([], baseline, 'pending')['state'] == 'not_researched'
    quote = {'kind': 'award', 'url': 'https://buyer.edu.cn/1', 'unit_price': 6800,
             'level': 'model_reference', 'gaps': ['configuration']}
    decision = price_decision([quote], baseline)
    assert decision['state'] == 'historical_reference'
    assert decision['fair_market_price_established'] is False
    assert '不能据此认定' in decision['summary']


def _historic(price, when, gaps, level='historical_configuration'):
    return {'kind': 'award', 'url': f'https://buyer.gov.cn/{price}', 'unit_price': price,
            'date': when, 'level': level, 'gaps': list(gaps)}


#: The real pair that exposed the problem: a genuine 35,200 award and a 186,000
#: runner-up bid that was wrongly pooled with it, 5.3x apart.
def test_a_wide_spread_never_becomes_a_price_band():
    """A 5.3x spread must be shown as individual records, not one 'range'."""
    baseline = {'model': 'H1850R', 'supplier_price': 40000, 'currency': 'CNY'}
    prices = [_historic(35200, '2022-12-16', ['configuration']),
              _historic(186000, '2021-01-08', ['configuration'])]

    summary = price_decision(prices, baseline)['summary']

    assert '35,200' in summary and '186,000' in summary
    assert '5.3 倍' in summary
    # No en-dash band may be asserted across records the system cannot reconcile.
    assert '35,200–186,000' not in summary
    assert '不能合并成一个价格区间' in summary


def test_close_prices_with_unverified_configuration_are_listed_not_banded():
    """Unverified configuration alone is enough to refuse a band."""
    baseline = {'model': 'H1850R', 'supplier_price': 36000, 'currency': 'CNY'}
    prices = [_historic(35000, '2022-12-16', ['configuration']),
              _historic(37000, '2021-01-08', ['configuration'])]

    summary = price_decision(prices, baseline)['summary']

    assert '35,000' in summary and '37,000' in summary
    assert '不能合并成一个价格区间' in summary
    assert '35,000–37,000' not in summary


def test_a_wide_spread_is_refused_even_when_configuration_is_known():
    """Dispersion is its own gate: a 5.3x spread is not one population."""
    baseline = {'model': 'H1850R', 'supplier_price': 40000, 'currency': 'CNY'}
    prices = [_historic(35200, '2022-12-16', []), _historic(186000, '2021-01-08', [])]

    summary = price_decision(prices, baseline)['summary']

    assert '5.3 倍' in summary
    assert '不能给出一个价格区间' in summary
    assert '35,200–186,000元（2条）' not in summary


def test_a_plausible_band_is_still_reported():
    """The safety gates must not remove the ordinary, well-matched case."""
    baseline = {'model': 'H1850R', 'supplier_price': 40000, 'currency': 'CNY'}
    prices = [_historic(35000, '2022-12-16', ['tax']), _historic(37000, '2021-01-08', ['tax'])]

    summary = price_decision(prices, baseline)['summary']

    assert '同型号历史成交范围35,000–37,000元（2条）' in summary
    assert '高于' in summary
    assert '不是当前市场公允价' in summary


def test_refusing_a_band_does_not_repeat_configuration_as_a_remaining_gap():
    """`configuration` is the reason for the refusal; listing it again is noise.

    迭代规范（2026-10）：剩下的缺口原来直接印内部字段名（``tax``、``delivery``…），英文 key
    漏进了用户可见文案；现在走 ``missing_condition_label`` 翻成中文（复用
    ``question_service.CONDITION_LABELS``）。断言跟着口径改，不是放宽。
    """
    baseline = {'model': 'H1850R', 'supplier_price': 36000, 'currency': 'CNY'}
    prices = [_historic(35000, '2022-12-16', ['configuration', 'tax']),
              _historic(37000, '2021-01-08', ['configuration', 'tax'])]

    summary = price_decision(prices, baseline)['summary']

    assert '税务（含税/未税及税率）' in summary
    assert '缺失条件：configuration' not in summary
    assert '缺失条件：税务（含税/未税及税率）' in summary
    assert 'tax' not in summary, '内部字段名又漏到用户可见的摘要里了'


def test_procurement_table_global_currency_keeps_unit_price_separate():
    from backend.services.procurement_tables import price_table_rows
    from backend.services.procurement_rules import validate_price
    body = '成交公告 2023-09-14 金额单位：元\n项目总额37799元\n|品牌|型号|数量|单价|\n|天津泰斯特|SPX-150BIII|1|6800|'
    row = price_table_rows(body, 'SPX-150BIII')[0]
    source = {'url': 'https://example.edu.cn/award', 'body_text': body, 'body_status': 'fetched', 'source_type': 'tender_document'}
    raw = {**row, 'kind': 'award', 'date': '2023-09-14'}
    base = {'brand': '泰斯特', 'model': 'SPX-150BIII', 'supplier_price': 6800, 'currency': 'CNY'}
    assert validate_price(raw, source, base)['unit_price'] == 6800
    assert validate_price({**raw, 'amount': 37799}, source, base) is None
    assert not price_table_rows(body.replace('SPX-150BIII', 'TBSPX-150BIII'), 'SPX-150BIII')


def test_supplier_revision_preserves_report_without_new_provider_calls(research):
    import copy
    service, run, provider, _ = research
    old = copy.deepcopy(service.report(run['task_id'], run['id']))
    def forbidden(*args, **kwargs):
        raise AssertionError('A supplier revision must reuse saved evidence')
    service.client_factory = forbidden
    provider.search = forbidden
    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500, 'warranty': '2年'})
    assert updated['baseline']['version'] == run['baseline']['version'] + 1
    assert updated['baseline']['warranty'] == '2年'
    # 教育／科研交易平台是第四个计数位，省级政采平台按型号直查是第五个。这里为 0
    # 表示这一轮没有为它们发过请求，与"没有任何新的 provider 调用"是同一件事
    # ——多这两位不等于多了调用。
    assert updated['usage'] == {'search': 0, 'fetch': 0, 'model': 0,
                                'education_platform': 0, 'province_award': 0}
    assert service.report(run['task_id'], run['id']) == old
    report = service.report(run['task_id'], updated['id'])
    assert report['parent_report_id'] == old['report_id']
    assert next(s for s in report['sections'] if s['key'] == 'decision')['summary'] == updated['price_decision']['summary']
    assert report['supplier_revision']['origin'] == 'user_supplied_supplier_statement'
    assert any(s['key'] == 'supplier_revision' for s in report['sections'])
    assert not any(s['key'] == 'supplement' for s in report['sections'])
    assert '已收到销售回复' in next(s for s in report['sections'] if s['key'] == 'decision')['items'][-1]['text']


def test_document_suggestion_does_not_become_user_fact_or_requirement(tmp_path):
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.procurement_intake import read_intake
    repo = SQLiteRepository(tmp_path / 'intake.db')
    repo.put('tasks', 't', {'id': 't', 'scenario': '水样检测'})
    repo.put('reports', 'a', {'kind': 'deepseek_analysis', 'created_at': '2026', 'candidates': [
        {'brand': 'B', 'model': 'PH10', 'sources': [{'document_id': 'd', 'excerpt': 'PH10'}]}]}, task_id='t')
    state = read_intake(repo, 't')
    assert state['draft']['model'] == 'PH10'
    assert state['facts'] == {} and state['parameters'] == []
    assert state['document_suggestions'] == {'brand': 'B', 'model': 'PH10'}
    assert repo.get('procurement_records', 'intake-t') is None
    repo.close()


def test_dimensionless_ph_accuracy_keeps_literal_value_and_rejects_invention():
    from backend.services.deepseek_analysis import _validated_field
    excerpt = 'Accuracy\npH: ± 0.01\nTemperature: ± 0.2°C'
    field = {'field_name': 'accuracy_sensitivity_and_repeatability',
             'value': {'pH准确度': '± 0.01', '虚构准确度': '± 0.001'},
             'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': excerpt}]}
    checked = _validated_field(field, {('d', 'page:1'): excerpt})
    assert checked['value'] == {'pH准确度': '± 0.01'}
    assert checked['_unsupported_parts'] == ['虚构准确度']


def test_grouped_parameter_labels_survive_model_projection():
    from backend.services.product_facts import table_facts
    html = '<table><tr><th colspan="2">型号</th><th>A10</th><th>A20</th></tr><tr><td rowspan="2">性能</td><td>使用温度范围</td><td>0~65℃</td><td>10~80℃</td></tr><tr><td>隔板层数</td><td>9层</td><td>12层</td></tr></table>'
    body = PublicWebSearchProvider._clean_html(html)
    facts = table_facts([{'url':'https://manufacturer.example/a', 'body_text':body, 'source_type':'manufacturer_website','body_status':'fetched'}], 'A10')
    assert any('使用温度范围 0~65' in f['text'] for f in facts)
    assert any('隔板层数 9层' in f['text'] for f in facts)


def test_product_fit_section_stops_judging_whether_requirements_are_met():
    """规范项（设计规范）：产品事实节只罗列资料原文。

    原来的 ``requirement_notes`` 会把采购要求与资料写法印在同一句里
    （「本轮要求 · 工作温度：比较基准记录的本轮要求是 25~42℃；本轮取得的资料里
    对应写法：…」），温度那一路还直接判「厂家温区覆盖本次使用范围」。这一节叫
    「产品事实与用途适配」，读者会把采购门槛读成这台机器的参数，所以那一层整块
    下线：没有逐字读到的原文，就不产出条目、也不写覆盖/未覆盖。
    """
    from backend.services import product_facts
    assert not hasattr(product_facts, 'requirement_notes')
    assert '只罗列' in product_facts.PRODUCT_FIT_BOUNDARY


def test_continue_after_alternatives_reopens_both_incomplete_lanes(research):
    service, run, _, _ = research
    run.update(active_lane='alternatives', status='partial', prices=[], alternatives=[])
    service.save(run)
    updated = service.resume(run['task_id'], run['id'])
    service.futures[run['id']].result(timeout=10)
    assert updated['active_lane'] == 'all'
    assert service.get(run['task_id'], run['id'])['lanes'] == {'price':'completed','alternatives':'completed'}


def test_historical_dimensions_do_not_override_current_manufacturer():
    from backend.services.product_facts import dimension_conflicts
    sources = [{'url':'https://manufacturer.example/a','body_status':'fetched','source_type':'manufacturer_website',
                'body_text':'[表格按列展开 行3列4] A20 内尺寸（宽*深*高mm） 500*400*750'},
               {'url':'https://buyer.edu.cn/a','body_status':'fetched','source_type':'tender_document',
                'body_text':'A20 工作室尺寸 450 × 450 × 550mm'}]
    conflict = dimension_conflicts(sources, 'A20')[0]
    assert len(conflict['evidence']) == 2 and '不能视为相同配置' in conflict['text']
    assert not dimension_conflicts(sources, 'TBA20')


def test_baidu_keeps_excluded_brand_operator():
    from backend.infrastructure.search_providers import BaiduSearchProvider
    assert BaiduSearchProvider.engine_query('培养箱 技术参数 -"原品牌"') == '培养箱 技术参数 -"原品牌"'


def test_new_domain_can_be_verified_after_brand_only_search(research, monkeypatch):
    service, run, _, _ = research
    from backend.services import source_identity
    monkeypatch.setattr(source_identity, 'verify_publisher', lambda *args: None)
    run['sources'] = []
    run['publisher_queries'] = []
    queries = []
    monkeypatch.setattr(service, 'query', lambda run, query, purpose: queries.append(query))
    service.verify_publishers(run, '测试品牌')
    run['sources'] = [{'url': 'https://manufacturer.example/product', 'source_type': 'unknown',
                       'body_text': '版权所有 测试品牌有限公司'}]
    service.verify_publishers(run, '测试品牌')
    service.verify_publishers(run, '测试品牌')
    assert len(queries) == 2
    assert queries[1] == '"manufacturer.example" (site:edu.cn OR site:gov.cn)'


def test_chat_supplier_confirmation_reuses_evidence(research):
    service, run, _, _ = research
    updated = service.confirm(run['task_id'], {**run['baseline'], 'intake_revision': 2,
                              'supplier_price': 7600, 'warranty': '2年'}, start=False)
    assert updated['evidence_reused'] is True
    assert updated['baseline']['intake_revision'] == 2
    assert updated['baseline']['supplier_price'] == 7600
    # 教育／科研交易平台是第四个计数位，省级政采平台按型号直查是第五个。这里为 0
    # 表示这一轮没有为它们发过请求，与"没有任何新的 provider 调用"是同一件事
    # ——多这两位不等于多了调用。
    assert updated['usage'] == {'search': 0, 'fetch': 0, 'model': 0,
                                'education_platform': 0, 'province_award': 0}


def test_supplier_chat_cannot_replace_recording_requirement(tmp_path):
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.procurement_intake import respond
    repo = SQLiteRepository(tmp_path / 'chat.db')
    repo.put('tasks', 't', {'id':'t', 'scenario':'培养'})
    original = {'name':'记录与导出', 'value':'自动连续记录并导出或打印', 'direction':'equal'}
    repo.put('procurement_records', 'intake-t', {'id':'intake-t', 'kind':'intake', 'task_id':'t', 'revision':1,
             'facts':{}, 'parameters':[original], 'deferred':[], 'messages':[], 'question':{'group':'review','text':'补充销售回复'}}, task_id='t')
    class Extractor:
        def analyze(self, **kwargs):
            return {'facts':{'recording':{'value':'CSV','quote':'可以导出CSV'},
                             'configuration':{'value':'记录模块','quote':'记录模块'}},
                    'parameters':[{'name':'导出格式','value':'CSV','quote':'可以导出CSV','direction':'equal'}]}
    state = respond(repo, 't', {'revision':1, 'message':'销售回复：包含记录模块，可以导出CSV'}, Extractor)
    assert state['parameters'] == [original]
    assert 'recording' not in state['facts']
    assert state['facts']['configuration']['value'] == '记录模块'
    repo.close()


def test_resume_uses_new_discovery_sources_in_bounded_context():
    from backend.services.research_planning import discovery_sources
    old = [{'url':f'https://old.example/{n}', 'purpose':'discovery', 'body_status':'fetched'} for n in range(9)]
    latest = {'url':'https://new-brand.example/a', 'purpose':'discovery', 'body_status':'fetched'}
    chosen = discovery_sources(old + [latest])
    assert chosen[0] == latest and len(chosen) == 8


def test_buyer_correction_in_mixed_sales_message_is_allowed():
    from backend.services.procurement_intake import requirement_quote_allowed
    message = '销售回复可以导出CSV。我的实验要求不变：需要自动连续记录并导出。'
    assert not requirement_quote_allowed(message, '可以导出CSV')
    assert requirement_quote_allowed(message, '需要自动连续记录并导出')
