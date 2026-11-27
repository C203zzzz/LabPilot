"""价格通道跑完之后才到货的正文：判定层判 PASS，但价格没落库。

实测根因（``设计说明`` §2.3）：

* ``prices()`` 里唯一的落库点是 ``run['prices'] = list(result)``——**赋值**，
  而 ``prices()`` 每个 target 都跑一遍，一次"什么都没抽到"的后续调用
  可以把已经通过全部判据的价格整条抹掉。``task-0a7f1ff94935`` 就是这样：
  ``model_results`` 里还存着正确的 ``XKA-2200``/17480，我用生产判据复算这条
  是 PASS，而 run 最终 ``prices == []``。
* ``prices()`` 只读它跑的那一刻的 ``run['sources']``，而 ``recover_sources``
  是最后一个会新增来源的步骤，跑在两个通道**都结束之后**。实测一轮 66 个来源
  里有 35 个是价格通道结束之后才抓回来的。``task-a283e5adf675`` 的
  ``sbm.pumc.edu.cn`` 公告（``tender_document``/``fetched``/8487B/含 ``DMi1``/
  金额窗口成立）就是这样：整个 run 的 ``model_results`` 里连一个 ``prices``
  键都没有，判定层从来没被问过。
"""
from backend.services.procurement_research import (
    LATE_PRICE_SOURCE_LIMIT, _merged_prices, _price_key,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
XKA_EXCERPT = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'
NOTICE_BODY = ('中标（成交）结果公告\n项目编号：YXZC2026-G1-00082-HCZB-0013\n'
               '采购人：峨山彝族自治县医疗与健康服务集团\n'
               '成交供应商：惠而好医疗科技（云南）有限公司\n' + XKA_EXCERPT + '\n')


def _extraction(**overrides):
    row = {'brand': '湘仪', 'model': 'XKA-2200', 'kind': 'award', 'amount': 17480,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'currency': 'CNY',
           'date': '2026-03-25', 'configuration': 'unknown', 'excerpt': XKA_EXCERPT}
    row.update(overrides)
    return row


def _late_source(url=NOTICE_URL):
    """一份现在四条件全满足、但**留痕里没有它**的来源 = 价格通道没见过它。"""
    return {'url': url, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': NOTICE_BODY, 'provider': 'ccgp'}


def _seen_source(url):
    """一份价格通道见过（但没抽到价的）来源：留痕里有它的 url。"""
    return {'url': url, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': NOTICE_BODY, 'provider': 'ccgp'}


def _extractor(service, row=None):
    calls = []

    class Client:
        endpoint = 'test://late'
        model = 'offline'
        last_usage = None

        def analyze(self, **kwargs):
            calls.append(kwargs.get('user_prompt', ''))
            return {'prices': [] if row is None else [dict(row)]}

    service.client_factory = Client
    return calls


def _baseline_on(run, model='XKA-2200', brand='湘仪'):
    """把基准指到公告里那台机器上。

    ``prices()`` 只在 ``identity(target['model']) == identity(baseline['model'])``
    时才会把价格写进 ``run['prices']``，而 fixture 的基准是另一台（````）。
    不指过来的话，测的就不是重扫逻辑，而是"基准对不上"这条既有规则。
    """
    run['baseline']['brand'] = brand
    run['baseline']['model'] = model
    return run['baseline']


# ---------------------------------------------------------------------------
# 落库：合并而不是覆盖
# ---------------------------------------------------------------------------

def test_merging_keeps_an_already_accepted_price_when_the_next_call_finds_nothing():
    """这就是 ``task-0a7f1ff94935`` 的形状：后一次调用返回空，价格整条消失。"""
    accepted = [{'url': NOTICE_URL, 'model': 'XKA-2200', 'unit_price': 17480,
                 'project_id': 'YXZC2026-G1-00082-HCZB-0013', 'lot': '', 'item': '',
                 'date': '2026-03-25'}]
    assert _merged_prices(accepted, []) == accepted
    assert _merged_prices([], accepted) == accepted


def test_merging_does_not_double_count_the_same_transaction():
    row = {'url': NOTICE_URL, 'model': 'XKA-2200', 'unit_price': 17480, 'date': '2026-03-25'}
    twinned = dict(row, excerpt='同一条的另一种写法')
    merged = _merged_prices([row], [twinned])
    assert len(merged) == 1


def test_merging_keeps_genuinely_different_rows():
    a = {'url': 'http://a/x', 'model': 'XKA-2200', 'unit_price': 17480, 'date': '2026-03-01'}
    b = {'url': 'http://b/y', 'model': 'XKA-2200', 'unit_price': 20800, 'date': '2026-04-01'}
    assert len(_merged_prices([a], [b])) == 2


def test_merging_tolerates_none_and_junk():
    assert _merged_prices(None, None) == []
    assert _merged_prices([None, 'x'], [{'url': 'http://a'}]) == [{'url': 'http://a'}]


# ---------------------------------------------------------------------------
# 晚到来源的重扫
# ---------------------------------------------------------------------------

def test_a_body_that_arrived_after_the_price_lane_is_read_now(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_late_source()]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert len(calls) == 1, '晚到的合格来源没有被重新读一次'
    assert [row['unit_price'] for row in added] == [17480]
    assert [row['unit_price'] for row in run['prices']] == [17480]


def test_a_source_the_price_lane_already_read_is_not_read_again(research):  # noqa: F811
    """代价有界：留痕里有它，就说明价格通道已经处理过，不再花第二次调用。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    source = _seen_source('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm')
    run['sources'] = [source]
    run['prices'] = []
    run['price_source_skips'] = [
        {'url': source['url'], 'target_model': run['baseline']['model'],
         'skip_reason': 'no_amount_evidence'}]
    calls = _extractor(service, _extraction())

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert calls == []
    assert added == []


def test_the_rescan_is_bounded_by_its_allowance(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_late_source(f'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t2026032{i}.htm')
                      for i in range(LATE_PRICE_SOURCE_LIMIT + 3)]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    service._rescan_late_price_sources(run, run['baseline'])

    assert len(calls) == LATE_PRICE_SOURCE_LIMIT


def test_a_body_without_the_target_model_does_not_spend_the_allowance(research):  # noqa: F811
    """上限只花在**有可能过闸门**的正文上。

    真环境补验暴露的：``task-0a7f1ff94935`` 里排在最前面的四个候选
    （``szc.xtu.edu.cn``、``ztb.usx.edu.cn``、两条 ``ggzy.haikou.gov.cn``）
    正文里压根没有 ``XKA-2200``——``prices()`` 在花模型调用**之前**就会以
    ``target_model_not_in_body`` 拒绝它们。它们照样各占一格上限，把唯一带着该型号
    成交单价的 CCGP 公告（合格候选里第 15 位）挤在门外，结果一次调用都没发生。
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    no_model = [{'url': f'https://szc.xtu.edu.cn/info/{i}.htm',
                 'source_type': 'tender_document', 'body_status': 'fetched',
                 'body_text': '中标（成交）结果公告\n项目编号：X\n成交供应商：某公司\n'}
                for i in range(LATE_PRICE_SOURCE_LIMIT * 2)]
    run['sources'] = no_model + [_late_source()]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert len(calls) == 1, '不含型号的正文不该占掉上限'
    assert [row['unit_price'] for row in added] == [17480]


def test_the_allowance_is_spent_on_tender_documents_first(research):  # noqa: F811
    """同为合格候选时先花在**公告**上，不按来源被追加进来的顺序。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    # 这些正文都含目标型号（所以都是合格候选），但只有最后那条是真公告。
    pages = [{'url': f'https://www.instrument.com.cn/p{i}.html',
              'source_type': 'manufacturer_website', 'body_status': 'fetched',
              'body_text': NOTICE_BODY} for i in range(LATE_PRICE_SOURCE_LIMIT)]
    run['sources'] = pages + [_late_source()]      # 公告排在最后
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert len(calls) == LATE_PRICE_SOURCE_LIMIT
    assert [row['unit_price'] for row in added] == [17480], '公告被非公告来源挤出了上限'


def test_the_rescan_is_idempotent(research):  # noqa: F811
    """报告读的时候也会走这一步，所以第二次不许再花模型调用。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_late_source()]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    first = service._rescan_late_price_sources(run, run['baseline'])
    second = service._rescan_late_price_sources(run, run['baseline'])

    assert len(calls) == 1
    assert first and second == []
    assert len(run['prices']) == 1


def test_the_rescan_does_not_loosen_the_acceptance_predicates(research):  # noqa: F811
    """重扫仍走 ``prices()``，所以 ``validate_price`` 照旧把关：改错型号不收。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_late_source()]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    _extractor(service, _extraction(model='别的型号-9'))

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert added == []
    assert run['prices'] == []


def test_the_rescan_does_nothing_when_nothing_is_late(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [{'url': 'https://www.instrument.com.cn/x.html', 'source_type': 'unknown',
                       'body_status': 'fetched', 'body_text': NOTICE_BODY}]
    run['prices'] = []
    run['price_source_skips'] = []
    _baseline_on(run)
    calls = _extractor(service, _extraction())

    assert service._rescan_late_price_sources(run, run['baseline']) == []
    assert calls == []


def test_a_price_already_on_the_run_survives_a_rescan_that_finds_nothing(research):  # noqa: F811
    """重扫不许把已经通过判据的价格弄丢——那正是它要修的病。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    already = {'url': 'http://cgzx.hust.edu.cn/jgfscg/27085.chtml', 'model': 'XKA-2200',
               'unit_price': 90000, 'kind': 'award', 'date': '2026-03-01', 'project_id': 'P-1'}
    run['sources'] = [_late_source()]
    run['prices'] = [dict(already)]
    run['price_source_skips'] = []
    _baseline_on(run)
    _extractor(service, None)          # 这次什么都没抽到

    service._rescan_late_price_sources(run, run['baseline'])

    assert [row['unit_price'] for row in run['prices']] == [90000]


def test_one_late_source_failure_does_not_block_the_next_source(research, monkeypatch):  # noqa: F811
    service, run, _, _ = research
    bad_url = 'https://broken.example/late?token=secret'
    good_url = 'https://good.example/late'
    run['sources'] = [_late_source(bad_url), _late_source(good_url)]
    run['prices'] = []
    run['price_source_skips'] = []
    run['price_source_cache_hits'] = []
    _baseline_on(run)
    calls = []

    def prices(view, _target):
        url = view['sources'][0]['url']
        calls.append(url)
        if url == bad_url:
            raise ValueError('读取失败 ' + bad_url)
        return [{'url': url, 'model': 'XKA-2200', 'unit_price': 17480,
                 'date': '2026-03-25'}]

    monkeypatch.setattr(service, 'prices', prices)

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert calls == [bad_url, good_url]
    assert [row['url'] for row in added] == [good_url]
    assert [row['url'] for row in run['prices']] == [good_url]
    assert run['unresolved_source_errors'] is True
    failure = run['failures'][-1]
    assert failure['code'] == 'source_skipped'
    assert bad_url.split('?')[0] in failure['error']
    assert 'token=secret' not in failure['error']

