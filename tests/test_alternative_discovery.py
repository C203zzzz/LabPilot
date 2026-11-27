# -*- coding: utf-8 -*-
"""Step four of the chain: 比较 -- a competitor has to be found somewhere else.

The comparison lane asked the model to find other instruments "in the given
bodies", but the bodies it handed over came from queries shaped like
``品类 参数 技术参数 型号``: manufacturer brochures about the *original* model.
Those pages contain no second brand, so the lane could only ever return
nothing, however good the model was.

Another brand's model number is published on *somebody else's* award notice,
inside the detail table that notice attaches. That corpus is only reachable if
the discovery round asks for it explicitly -- and it has to be handed to the
model ahead of the brochures, because the model sees a bounded number of them.
"""
from backend.services.procurement_research import ON_DEMAND_ENV
from backend.services.research_planning import discovery_queries, discovery_relevant, discovery_sources
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

BASELINE = {
    'brand': '甲厂', 'model': 'Q10', 'category': '生物安全柜', 'scenario': '微生物检测',
    'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}],
}

BROCHURE = {
    'url': 'https://acme-lab.com/Q10', 'purpose': 'official', 'body_status': 'fetched',
    'source_type': 'manufacturer_website',
    'body_text': '甲厂Q10 生物安全柜 技术参数 容量 100 ml 型号 Q10',
}
# Another hospital's award notice, and the price table it attaches. The
# competitor exists only in that attachment.
NOTICE = {
    'url': 'https://hospital-a.example.com/notice/2026-09-11', 'purpose': 'discovery',
    'body_status': 'fetched', 'source_type': 'tender_document', 'title': '生物安全柜采购项目中标（成交）结果公告',
    'body_text': '生物安全柜采购项目中标（成交）结果公告\n采购人：某某医院\n附件信息：\n[报价明细](files/detail.xlsx)',
}
NOTICE_TABLE = {
    'url': 'https://hospital-a.example.com/notice/files/detail.xlsx', 'purpose': 'discovery',
    'body_status': 'fetched', 'source_type': 'tender_document', 'parent_url': NOTICE['url'],
    'body_text': '货物名称 | 规格型号 | 数量 | 单价（元）\n生物安全柜 | 乙厂Q20 | 1 | 56,000\n',
    'publication_context': {'url': NOTICE['url'], 'body_text': NOTICE['body_text'],
                            'linked_url': 'https://hospital-a.example.com/notice/files/detail.xlsx'},
}


def test_discovery_asks_the_publication_corpus_for_other_brands():
    queries = discovery_queries(BASELINE)
    publication = queries[-1]
    assert '生物安全柜' in publication and '中标' in publication and '报价明细' in publication
    assert '技术参数' not in publication          # it asks for notices, not brochures
    assert '-"甲厂"' in publication               # the original brand stays excluded


def test_another_notices_table_outranks_brochures_about_the_original_model():
    sources = discovery_sources([BROCHURE, NOTICE, NOTICE_TABLE])
    assert NOTICE_TABLE in sources and NOTICE in sources
    # Brochures never crowd the notice out of the model's bounded context.
    assert sources.index(NOTICE_TABLE) < sources.index(BROCHURE)


def test_discovery_source_order_is_reproducible_when_provider_order_changes():
    sources = [
        {**BROCHURE, 'url': 'https://ACME-LAB.com/Q10/?utm_source=search'},
        {**NOTICE_TABLE, 'url': NOTICE_TABLE['url'] + '?utm_medium=search'},
        {**NOTICE, 'url': NOTICE['url'] + '/'},
    ]

    first = discovery_sources(sources, limit=None, baseline=BASELINE)
    second = discovery_sources(list(reversed(sources)), limit=None, baseline=BASELINE)

    assert [source['url'] for source in first] == [source['url'] for source in second]
    assert [source['source_type'] for source in first] == [
        'tender_document', 'tender_document', 'manufacturer_website']


def test_same_type_discovery_sources_use_normalized_url_as_a_stable_tiebreaker():
    sources = [
        {**BROCHURE, 'url': 'https://vendor.example/b/?utm_source=search',
         'title': 'same title', 'snippet': '', 'body_text': 'same body'},
        {**BROCHURE, 'url': 'https://VENDOR.example/a/',
         'title': 'same title', 'snippet': '', 'body_text': 'same body'},
    ]

    first = discovery_sources(sources, limit=None, baseline=BASELINE)
    second = discovery_sources(list(reversed(sources)), limit=None, baseline=BASELINE)

    assert [source['url'] for source in first] == [
        'https://VENDOR.example/a/', 'https://vendor.example/b/?utm_source=search']
    assert [source['url'] for source in second] == [
        'https://VENDOR.example/a/', 'https://vendor.example/b/?utm_source=search']


def test_award_notices_that_name_the_category_pass_the_pre_read_filter():
    assert discovery_relevant(NOTICE, BASELINE) is True
    # Reverse: widening must not accept an off-topic notice.
    assert discovery_relevant({'title': '某市道路工程施工中标结果公告', 'snippet': ''}, BASELINE) is False
    assert discovery_relevant({'title': '办公用品采购项目中标公告', 'snippet': ''}, BASELINE) is False


def test_a_competitor_only_present_in_another_notices_table_reaches_verification(research, monkeypatch):  # noqa: F811
    # 末两条断言读的是核验产物（``matrix`` / ``status``），只有**批量查价**那条路会建。
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    run.update({'baseline': BASELINE, 'sources': [BROCHURE, NOTICE, NOTICE_TABLE],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': []})
    prompts = []

    def fake_model(run_, prompt, data):
        prompts.append(prompt)
        if '其他仪器' in prompt:
            # The model may only propose what the supplied bodies contain.
            offered = {s['url'] for s in data['sources']}
            assert NOTICE_TABLE['url'] in offered
            assert BROCHURE['url'] not in offered or len(prompts) == 1
            return {'candidates': [{'brand': '乙厂', 'model': 'Q20',
                                    'url': NOTICE_TABLE['url'],
                                    # 2026-10-06 起"找不到配置的候选不展示"，配置为空的
                                    # 候选会在收尾时被撤下（``tests/test_m4_pipeline.py``）；
                                    # 这一条测的是核验产物，所以给它一段正文里逐字存在的配置。
                                    'configuration': '生物安全柜 | 乙厂Q20 | 1 | 56,000',
                                    'excerpt': '生物安全柜 | 乙厂Q20 | 1 | 56,000'}]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)

    assert [c['model'] for c in run['alternatives']] == ['Q20']
    verified = run['alternatives'][0]
    assert verified['brand'] == '乙厂'
    assert verified['matrix'] and verified['matrix'][0]['status'] == 'unknown'   # no evidence yet, not a claim
    assert verified['status'] == 'needs_review'


def test_a_competitor_quoted_from_a_page_that_does_not_contain_it_is_dropped(research, monkeypatch):  # noqa: F811
    """Reverse assertion: the excerpt must exist in the cited body."""
    service, run, _, _ = research
    run.update({'baseline': BASELINE, 'sources': [BROCHURE, NOTICE_TABLE],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': []})

    def fake_model(run_, prompt, data):
        if '其他仪器' in prompt:
            return {'candidates': [{'brand': '乙厂', 'model': 'Q99',
                                    'url': BROCHURE['url'],       # brochure never names Q99
                                    'excerpt': '生物安全柜 | 乙厂Q99 | 1 | 1'}]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)
    # 造假的那条必须被丢掉：``excerpt`` 在被引用的正文里根本不存在。这一条**不许放松**。
    assert not any(c.get('model') == 'Q99' for c in run['discovered_candidates']), run['discovered_candidates']
    # 品牌没解析出来的兜底候选会补一次核验、进入可比较名单（设计规范：品牌不该
    # 是拦路虎），所以这里不再断言名单为空；要守的是"编造的那条没进来"。
    assert not any(c.get('model') == 'Q99' for c in run['alternatives'])
    # 凡是被列出来的候选，`excerpt` 都必须是原文里逐字存在的，且都自报家门为兜底产物。
    if run['discovered_candidates']:
        assert all(c.get('proposed_by') == 'text_scan' for c in run['discovered_candidates'])
        assert all(c['excerpt'] in source['body_text']
                   for c in run['discovered_candidates'] for source in run['sources']
                   if source['url'] == c['url'])
