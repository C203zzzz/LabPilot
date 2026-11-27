from backend.domain.search import SearchQuery, SearchResult, BodyStatus, PriceEvidenceLevel, SourceType, normalize_url
from backend.services.official_search import FakeSearchProvider, OfficialSearchService, classify_source, queries_from_candidate
from backend.infrastructure.search_providers import (
    BaiduSearchProvider,
    GroundedSearchProvider,
    LocalIndexSearchProvider,
    PublicWebSearchProvider,
)

def test_normalize_and_dedupe():
    assert normalize_url('HTTPS://ThermoFisher.com/p/?utm_source=x#frag') == 'https://thermofisher.com/p'
    q=SearchQuery('Q500')
    rs=[SearchResult('https://www.thermofisher.com/a/?utm_medium=x','A','summary'), SearchResult('https://www.thermofisher.com/a','A2','other')]
    run=OfficialSearchService(FakeSearchProvider({q.text:rs},{'https://www.thermofisher.com/a':'full text'})).run(q)
    assert len(run.sources)==1 and run.sources[0].body_status is BodyStatus.FETCHED

def test_authority_fixture_types():
    assert classify_source('https://www.eppendorf.com/download/manual.pdf') is SourceType.MANUFACTURER_DOWNLOAD
    assert classify_source('https://dealer.example.com/p') is SourceType.UNKNOWN
    assert classify_source('https://www.fishersci.com/product') is SourceType.DEALER

def test_summary_is_not_body_and_failures_observable():
    q=SearchQuery('Q500'); r=SearchResult('https://www.eppendorf.com/p','P','summary')
    run=OfficialSearchService(FakeSearchProvider({q.text:[r]})).run(q)
    assert run.sources[0].body_text is None and run.sources[0].body_status is BodyStatus.FAILED
    assert any('body_failed' in w for w in run.warnings)

def test_candidate_query_adapter():
    class C: brand='Tecan'; model='INFINITE 200 PRO'; id='c1'
    qs=queries_from_candidate(C()); assert qs[0].candidate_id=='c1' and 'Tecan INFINITE 200 PRO' in qs[0].text


def test_candidate_query_adapter_uses_business_purpose_terms():
    class C: brand='Tecan'; model='INFINITE 200 PRO'; id='c1'
    tender = queries_from_candidate(C(), purpose='tender')[0]
    discovery = queries_from_candidate(C(), purpose='discovery')[0]
    assert tender.purpose == 'tender' and '中标 成交公告 型号 单价' in tender.text
    assert discovery.purpose == 'discovery' and '同类产品 具体型号 技术参数' in discovery.text

def test_multi_provider_fallback_and_provenance():
    q=SearchQuery('multi')
    bad=FakeSearchProvider(failures={'multi': TimeoutError('down')}); bad.name='down'
    good=FakeSearchProvider({'multi':[SearchResult('https://www.eppendorf.com/p','P','s')]},{'https://www.eppendorf.com/p':'body'}); good.name='brave'
    other=FakeSearchProvider({'multi':[SearchResult('https://www.eppendorf.com/p','P2','s2')]}); other.name='tavily'
    run=OfficialSearchService(providers=[bad,good,other]).run(q)
    assert len(run.sources)==1 and set(run.sources[0].provider_provenance)=={'brave','tavily'}
    assert any('provider_failed:down' in w for w in run.warnings)


def test_body_fetch_uses_result_provider_without_cross_provider_transport():
    q = SearchQuery('multi source')
    calls = []
    first = FakeSearchProvider({'multi source': []})
    first.name = 'first'
    first.fetch = lambda url, timeout: calls.append(('first', url)) or 'wrong body'
    second = FakeSearchProvider({
        'multi source': [SearchResult('https://www.eppendorf.com/p', 'P', provider='second')]
    })
    second.name = 'second'
    second.fetch = lambda url, timeout: calls.append(('second', url)) or 'correct body'

    run = OfficialSearchService(providers=[first, second]).run(q)

    assert run.sources[0].body_text == 'correct body'
    assert calls == [('second', 'https://www.eppendorf.com/p')]


def test_discovery_only_result_falls_back_to_the_fetch_only_transport():
    """A model/Tavily citation still has a body: the runtime's fetch-only client.

    The runtime appends a transport-only provider (``supports`` refuses every
    query) because discovery providers own no transport. Selecting a fetcher by
    the result's provider name alone silently dropped that client.
    """
    q = SearchQuery('model citation')
    calls = []

    class DiscoveryOnly:
        name = 'deepseek_web'
        stop_on_results = True

        def supports(self, _query):
            return True

        def search(self, _query):
            return [SearchResult('https://buyer.example.com/award', '成交公告', provider='deepseek_web')]

    class FetchOnly:
        name = 'transport'
        fetch_calls = calls

        def supports(self, _query):
            return False

        def fetch(self, url, timeout):
            calls.append(url)
            return '完整正文 单价1000元'

    run = OfficialSearchService(providers=[DiscoveryOnly(), FetchOnly()]).run(q)

    assert calls == ['https://buyer.example.com/award']
    assert run.sources[0].body_text == '完整正文 单价1000元'
    assert run.sources[0].body_status is BodyStatus.FETCHED


def test_baidu_failure_does_not_block_public_fallback_and_body_gate_stays_strict():
    q = SearchQuery('Q500 成交', purpose='price')
    baidu = BaiduSearchProvider(transport=lambda *_args: (_ for _ in ()).throw(RuntimeError('temporary outage')))
    public = PublicWebSearchProvider(
        transport=lambda url, timeout: (
            '<html>Q500 成交公告，预算 12000 元</html>'.encode(), 'text/html'
        ) if url.endswith('/award') else (
            '<li class="b_algo"><h2><a href="https://buyer.edu.cn/award">成交公告</a></h2></li>'.encode(),
            'text/html',
        )
    )
    run = OfficialSearchService(providers=[baidu, public], fetch_body=True).run(q)
    assert any(w.startswith('provider_failed:baidu:') for w in run.warnings)
    assert run.sources and run.sources[0].body_status is BodyStatus.FETCHED
    assert run.sources[0].price_evidence_level is PriceEvidenceLevel.HISTORICAL_TENDER
    assert run.sources[0].price_amount is None
    assert run.sources[0].price_quote is None


def test_official_search_uses_injected_page_reader_before_provider_fetch():
    from types import SimpleNamespace
    q = SearchQuery('Q500 official', purpose='official')
    row = SearchResult('https://www.eppendorf.com/q500', 'Q500 official')
    calls = []
    provider = PublicWebSearchProvider(
        transport=lambda url, timeout: (_ for _ in ()).throw(TimeoutError('provider fetch failed'))
    )
    provider.search = lambda query: [row]
    reader = SimpleNamespace(read=lambda url, timeout: (
        calls.append((url, timeout)) or SimpleNamespace(
            text='Q500 official rendered specifications', payload=b'<html/>', content_type='text/html',
        )
    ))
    run = OfficialSearchService(providers=[provider], reader=reader, fetch_body=True).run(q)
    assert run.sources[0].body_status is BodyStatus.FETCHED
    assert run.sources[0].body_text == 'Q500 official rendered specifications'
    assert calls == [('https://www.eppendorf.com/q500', 8.0)]

def test_price_evidence_layers_and_unknown_conditions():
    q=SearchQuery('price')
    rs=[SearchResult('https://vendor.example/tender/采购结果.pdf','中标结果公告','报价 10000 元'), SearchResult('https://www.eppendorf.com/p','官方产品页','公开价格 12000'), SearchResult('https://www.fishersci.com/p','经销商','10000')]
    run=OfficialSearchService(FakeSearchProvider({'price':rs}, {rs[0].url:'中标结果公告 成交价 10000 元', rs[1].url:'官方产品正文', rs[2].url:'经销商正文'})).run(q)
    levels={s.source_type.value:s.price_evidence_level.value for s in run.sources}
    assert levels['tender_document']=='historical_tender' and levels['manufacturer_website']=='official_public' and levels['dealer']=='dealer_lead'
    tender = next(source for source in run.sources if source.source_type is SourceType.TENDER_DOCUMENT)
    assert tender.currency == 'CNY' and tender.tax_condition is None
    assert all(s.price_evidence_level.value!='official_public' for s in run.sources if s.source_type.value=='search_snippet')

def test_grounded_baidu_and_local_provider_offline_merge():
    q=SearchQuery('Eppendorf Q500', purpose='discovery')
    grounded=GroundedSearchProvider(client=lambda _: [{'url':'https://www.eppendorf.com/Q500/?utm_source=model','title':'Q500','snippet':'model summary'}])
    baidu=BaiduSearchProvider(transport=lambda url,timeout: (b'<div class="result c-container" mu="https://www.eppendorf.com/Q500"><h3 class="t"><a href="#">Q500 official</a></h3><span>baidu summary</span></div>', 'text/html'))
    local=LocalIndexSearchProvider([{'url':'https://www.fishersci.com/q500','title':'Eppendorf Q500 dealer','snippet':'local fixture'}])
    run=OfficialSearchService(providers=[grounded,baidu,local], fetch_body=False).run(q)
    assert len(run.sources)==2
    official=next(s for s in run.sources if s.source_type is SourceType.MANUFACTURER_WEBSITE)
    assert set(official.provider_provenance)=={'grounded_model','baidu'}
    assert official.body_text is None and official.body_status is BodyStatus.NOT_REQUESTED

def test_missing_real_provider_config_is_warning_and_local_fallback_continues():
    q=SearchQuery('Tecan INFINITE 200 PRO')
    grounded=GroundedSearchProvider()
    local=LocalIndexSearchProvider([{'url':'https://www.tecan.com/infinite-200-pro','title':'Tecan INFINITE 200 PRO','snippet':'fixture only'}])
    run=OfficialSearchService(providers=[grounded,local], fetch_body=False).run(q)
    assert len(run.sources)==1
    assert any('provider_failed:grounded_model' in w and 'not configured' in w for w in run.warnings)


def test_public_web_provider_parses_discovery_and_fetches_body_without_key(monkeypatch):
    search_html = b'''<html><li class="b_algo"><h2><a href="https://www.tecan.com/infinite-200-pro">Infinite 200 PRO</a></h2><p>Official product page</p></li></html>'''
    bodies = {
        'https://www.bing.com/search?q=Tecan+Infinite+200+PRO&count=3': (search_html, 'text/html'),
        'https://www.tecan.com/infinite-200-pro': (b'<html><main>Official specifications and temperature control</main></html>', 'text/html'),
    }

    # 禁用 Jina Reader 以确保使用 mock 的 transport
    monkeypatch.setenv("ENABLE_JINA_READER", "false")

    provider = PublicWebSearchProvider(transport=lambda url, timeout: bodies[url])
    run = OfficialSearchService(providers=[provider], fetch_body=True).run(SearchQuery('Tecan Infinite 200 PRO', limit=3))
    assert len(run.results) == 1
    assert run.sources[0].body_status is BodyStatus.FETCHED
    assert 'no_official_source' not in run.warnings
    assert 'temperature control' in run.sources[0].body_text


def test_public_web_provider_filters_irrelevant_chinese_tender_results_and_builds_tender_query():
    seen = []
    pages = {
        'https://www.bing.com/search?q=%22%E9%85%B6%E6%A0%87%E4%BB%AA%22+%E4%B8%AD%E6%A0%87+%E6%88%90%E4%BA%A4+%E9%87%87%E8%B4%AD%E7%BB%93%E6%9E%9C+filetype%3Apdf&count=4': '''<li class="b_algo"><h2><a href="https://gov.example.cn/中标结果.pdf">酶标仪中标结果公告</a></h2><p>采购结果 成交价</p></li><li class="b_algo"><h2><a href="https://dict.example/酶">酶的词典</a></h2><p>百科</p></li>'''.encode('utf-8'),
        'https://www.bing.com/search?q=%22%E9%85%B6%E6%A0%87%E4%BB%AA%22+%E6%8B%9B%E6%A0%87%E5%85%AC%E5%91%8A+%E9%87%87%E8%B4%AD%E5%85%AC%E5%91%8A&count=4': b"",
    }
    def transport(url, timeout):
        seen.append(url)
        return pages[url], 'text/html'
    provider = PublicWebSearchProvider(transport=transport)
    rows = provider.search(SearchQuery('酶标仪 中标公告 PDF', purpose='price', limit=4))
    assert len(rows) == 1
    assert '中标结果公告' in rows[0].title
    assert len(seen) == 2


def test_tender_without_verified_body_is_not_historical_price():
    q=SearchQuery('price')
    row=SearchResult('https://vendor.example/tender/采购结果.pdf','中标结果公告','报价 10000 元')
    run=OfficialSearchService(FakeSearchProvider({'price':[row]}, {row.url:'普通产品介绍'})).run(q)
    assert run.sources[0].price_evidence_level.value == 'unknown'
    assert any('tender_body_unverified' in w for w in run.warnings)


def test_formal_contract_body_is_historical_tender_evidence():
    row = SearchResult('https://www7.zzu.edu.cn/ztb/info/1136/11076.htm', '设备采购合同', '线索')
    body = ('合同名称：设备采购项目\n'
            '设备/服务名称 | 服务内容 | 数量 | 单价(元) | 合计(元)\n'
            '低温培养箱 | SPX-150BIII | 2 | 5800 | 11600')
    run = OfficialSearchService(FakeSearchProvider({'contract': [row]}, {row.url: body})).run(SearchQuery('contract', purpose='price'))
    source = run.sources[0]
    assert source.price_evidence_level.value == 'historical_tender'
    assert source.body_status is BodyStatus.FETCHED
    assert not any('tender_body_unverified' in w for w in run.warnings)


def test_tender_metadata_extracts_explicit_amount_and_date_only():
    row = SearchResult('https://www.ccgp.gov.cn/cggg/dfgg/zbgg/result.htm', '多功能酶标仪中标公告', '线索')
    body = '多功能酶标仪中标公告 2024年12月11日 总中标金额：￥108.800000 万元（人民币）'
    run = OfficialSearchService(FakeSearchProvider({'metadata': [row]}, {row.url: body})).run(SearchQuery('metadata', purpose='price'))
    source = run.sources[0]
    assert source.price_evidence_level.value == 'historical_tender'
    assert source.publication_date == '2024-12-11'
    assert source.currency == 'CNY'
    assert source.price_amount == 1088000
    assert source.price_unit == '万元'
    assert '108.800000' in source.price_quote


def test_tender_amount_extracts_from_real_ccgp_table_shapes():
    # Observed on search.ccgp.gov.cn award pages after table linearization.
    row = SearchResult('https://www.ccgp.gov.cn/cggg/dfgg/gzgg/result.htm', '生物安全柜成交公告', '线索')
    body = ('深圳大学公开招标公告 2026年9月15日'
            '1 | 三、中标(成交)信息 | 1.中标结果： 中标（成交）金额(元)： 报价：836000（元） 中标供应商名称： 广州前师科技有限公司')
    run = OfficialSearchService(FakeSearchProvider({'metadata': [row]}, {row.url: body})).run(SearchQuery('metadata', purpose='price'))
    source = run.sources[0]
    assert source.price_amount == 836000
    assert source.price_unit == '元'
    assert '836000' in source.price_quote


def test_tender_amount_in_table_with_pipe_separator_is_extracted():
    row = SearchResult('https://www.ccgp.gov.cn/cggg/dfgg/zbgg/result2.htm', '仪器采购项目中标(成交)结果公告', '线索')
    body = ('结果公告 2026年9月18日'
            '总中标金额 | ￥73.523000 万元（人民币） | ￥73.523000 万元（人民币）')
    run = OfficialSearchService(FakeSearchProvider({'metadata': [row]}, {row.url: body})).run(SearchQuery('metadata', purpose='price'))
    source = run.sources[0]
    assert source.price_amount == 735230
    assert source.price_unit == '万元'


def test_voided_award_amount_is_not_extracted_as_historical_price():
    row = SearchResult('https://www.ccgp.gov.cn/cggg/dfgg/gzgg/result3.htm', '生物安全柜成交公告', '线索')
    body = ('成交公告 2026年9月14日'
            '1.中标结果： 中标（成交）金额(元)： 报价：836000（元） 中标供应商名称： 某公司'
            '| 根据柳州市财政局文件监督检查处理决定书（柳财采〔2026〕36号）的处理结果，本项目废标，重新组织采购活动。')
    run = OfficialSearchService(FakeSearchProvider({'metadata': [row]}, {row.url: body})).run(SearchQuery('metadata', purpose='price'))
    source = run.sources[0]
    assert source.price_amount is None
    assert source.price_quote is None


def test_ccgp_domain_and_result_title_variants_classify_as_tender():
    assert classify_source('http://www.ccgp.gov.cn/cggg/dfgg/qtgg/202609/t1.htm', '其他公告') is SourceType.TENDER_DOCUMENT
    assert classify_source('http://province.example.com/news/1', '某项目中标(成交)结果公告') is SourceType.TENDER_DOCUMENT
    assert classify_source('http://province.example.com/news/2', '某项目成交结果公告') is SourceType.TENDER_DOCUMENT
