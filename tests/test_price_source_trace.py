"""价格通道入口的留痕：每条被入口筛掉的来源，都要说得出被哪一条筛掉的。

背景（``设计说明`` §2.2）：``prices()`` 的入口有三条判据
（``body_status == 'fetched'`` / ``source_type`` 在白名单内 / ``contains_model(body,
target)``），外加循环里两条成本门（金额门、每 target 4 条上限）。五条**全部静默**，
所以"这份公告从没被读过"和"这份公告什么价都没抽到"在库里的形状完全一样。
隔离库实测：18 个 run 的 590 条有正文来源里，入口只把 13 条送进了提取流程——
另外 577 条被筛掉时一条记录都没留。

这一层只加留痕，不改任何一个采信决定。
"""
from backend.services.price_rejections import (
    PRICE_SOURCE_TYPES, SKIP_BODY_NOT_FETCHED, SKIP_MODEL_NOT_IN_BODY,
    SKIP_NO_AMOUNT_EVIDENCE, SKIP_SOURCE_LIMIT, SKIP_TYPE_NOT_PRICE_SOURCE,
    price_source_candidates, price_source_skip_record, price_source_trace_summary,
)
from backend.services.procurement_rules import contains_model
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


def _src(url, body_status='fetched', source_type='tender_document', body=''):
    return {'url': url, 'body_status': body_status, 'source_type': source_type,
            'body_text': body}


NOTICE = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'


def test_the_three_entry_predicates_are_applied_in_the_old_order():
    """``selected`` 必须与改动前那句推导式逐条一致。"""
    sources = [
        _src('http://a.gov.cn/1', body=NOTICE),                       # 入选
        _src('http://b.com/2', source_type='unknown', body=NOTICE),   # 类型挡
        _src('http://c.com/3', body='与本型号无关的正文'),             # 型号挡
        _src('http://d.com/4', body_status='failed', body=''),        # 未抓到挡
        _src('http://e.gov.cn/5', body=NOTICE),
    ]
    selected, trace = price_source_candidates(sources, {'model': 'XKA-2200'})

    expected = [s for s in sources if s['body_status'] == 'fetched'
                and s['source_type'] in PRICE_SOURCE_TYPES
                and contains_model(s['body_text'], 'XKA-2200')]
    assert selected == expected
    assert [s['url'] for s in selected] == ['http://a.gov.cn/1', 'http://e.gov.cn/5']


def test_every_dropped_source_names_the_predicate_that_dropped_it():
    sources = [
        _src('http://b.com/2', source_type='unknown', body=NOTICE),
        _src('http://c.com/3', body='与本型号无关的正文'),
        _src('http://d.com/4', body_status='failed', body=''),
    ]
    _, trace = price_source_candidates(sources, {'model': 'XKA-2200'})

    assert [item['skip_reason'] for item in trace] == [
        SKIP_TYPE_NOT_PRICE_SOURCE, SKIP_MODEL_NOT_IN_BODY, SKIP_BODY_NOT_FETCHED]
    for item in trace:
        assert item['target_model'] == 'XKA-2200'
        assert item['source_type'] and item['body_status']
        assert isinstance(item['body_len'], int)


def test_the_trace_carries_enough_to_audit_without_reopening_the_source():
    """报告要能只说事实：哪条 URL、什么类型、正文多长、被哪一条挡的。"""
    record = price_source_skip_record(
        _src('https://sbm.pumc.edu.cn/xwzx/cggg/485241.htm', body='x' * 120),
        {'model': 'DMi1'}, SKIP_TYPE_NOT_PRICE_SOURCE)
    assert record == {
        'url': 'https://sbm.pumc.edu.cn/xwzx/cggg/485241.htm',
        'host': 'sbm.pumc.edu.cn',
        'source_type': 'tender_document',
        'body_status': 'fetched',
        'body_len': 120,
        'target_model': 'DMi1',
        'skip_reason': SKIP_TYPE_NOT_PRICE_SOURCE,
    }


def test_an_unparseable_url_does_not_raise():
    record = price_source_skip_record({'url': None, 'body_text': None}, {'model': 'X'},
                                      SKIP_BODY_NOT_FETCHED)
    assert record['host'] == '' and record['body_len'] == 0


def test_the_summary_counts_each_reason_once_and_in_a_stable_order():
    trace = [
        {'skip_reason': SKIP_SOURCE_LIMIT}, {'skip_reason': SKIP_SOURCE_LIMIT},
        {'skip_reason': SKIP_MODEL_NOT_IN_BODY}, {'skip_reason': SKIP_NO_AMOUNT_EVIDENCE},
    ]
    assert price_source_trace_summary(trace) == [
        {'skip_reason': SKIP_MODEL_NOT_IN_BODY, 'count': 1},
        {'skip_reason': SKIP_NO_AMOUNT_EVIDENCE, 'count': 1},
        {'skip_reason': SKIP_SOURCE_LIMIT, 'count': 2},
    ]


def test_an_empty_source_list_is_not_an_error():
    assert price_source_candidates([], {'model': 'X'}) == ([], [])
    assert price_source_candidates(None, None) == ([], [])


# ---------------------------------------------------------------------------
# 端到端：真正的 prices() 必须把留痕写在 run 上（不然报告和重放都读不到）
# ---------------------------------------------------------------------------

def test_prices_writes_the_entry_trace_on_the_run(research):  # noqa: F811
    """隔离库里 a283 那份 Leica DMi1 公告的形状：正文在、类型对、型号在。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    service.model = lambda run, prompt, data: {'prices': []}
    run['sources'] = [
        _src('https://sbm.pumc.edu.cn/xwzx/cggg/485241.htm', body=NOTICE),
        _src('https://www.instrument.com.cn/show/price/C340954.html',
             source_type='unknown', body=NOTICE),
        _src('https://www.nxu.edu.cn/a.pdf', body='这台设备与目标型号无关'),
    ]

    service.prices(run, {'brand': '湘仪', 'model': 'XKA-2200'})

    skips = run.get('price_source_skips')
    assert skips, '价格通道的入口留痕没有写回 run'
    by_url = {item['url']: item['skip_reason'] for item in skips}
    assert by_url['https://www.instrument.com.cn/show/price/C340954.html'] == SKIP_TYPE_NOT_PRICE_SOURCE
    assert by_url['https://www.nxu.edu.cn/a.pdf'] == SKIP_MODEL_NOT_IN_BODY
    assert 'https://sbm.pumc.edu.cn/xwzx/cggg/485241.htm' not in by_url


def test_the_trace_does_not_erase_another_targets_trace(research):  # noqa: F811
    """同一个 run 会被主型号和每个替代候选各跑一遍 prices()，留痕要能分开。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    service.model = lambda run, prompt, data: {'prices': []}
    run['sources'] = [_src('https://www.instrument.com.cn/x.html', source_type='unknown',
                           body='XKA-2200 与 LS-1 都在这页上')]

    service.prices(run, {'brand': '湘仪', 'model': 'XKA-2200'})
    first = len(run['price_source_skips'])
    assert first

    service.prices(run, {'brand': '路迈', 'model': 'LS-1'})

    targets = {item['target_model'] for item in run['price_source_skips']}
    assert targets == {'XKA-2200', 'LS-1'}
