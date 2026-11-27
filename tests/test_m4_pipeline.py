"""M4：主流水线四条口径的落点（设计规范 + 点名案例）。

依据是 ``回归基线``（§Q2 拒收粒度、
§Q3a 找不到配置就不展示、§Q6 展示链接与抽取页不一致）与 ``01-回归基线.json``；
三条案例的真实台账在 ``output/e2e-final/runs/``（``b3-r8-median``、
``b3-opt-balance-mt-me204e``、``b3-opt-cleanbench-boxun-bj1cd``）。

这里钉住的是**判据与落点**，不是某一次真跑的输出：每条断言都用台账里逐字抄下来的
字段值（URL、型号、金额、日期、条数），所以改动若把口径改回去，这些用例会红。
"""
from backend.services.procurement_research import (
    REJECTION_SOURCE_AGGREGATE_MIN, SUSPECTED_SAME_TRANSACTION_REASON,
    _merge_price_rows, aggregate_rejections_by_source, align_candidate_source_url,
    annotate_suspected_same_transaction, candidate_has_configuration,
    candidate_verification_source, withdraw_unconfigured_candidates,
)

# §Q6 的真实两条地址：634 页写的是 BBS-H1300，630 页写的是 BBS-V600。
OLAIBO_DISCOVERED = 'https://www.olaibo.com/ProductDetail/634.html'
OLAIBO_CONFIGURED = 'https://www.olaibo.com/ProductDetail/630.html'

# §Q6 的候选：型号 bekomat30 在两个页面上都不存在，配置抽自 630 页。
BEKOMAT = {
    'model': 'bekomat30',
    'brand': '济南欧莱博科学仪器有限公司',
    'url': OLAIBO_DISCOVERED,
    'excerpt': 'bekomat30 [http://www.sh-gjdq.com]',
    'configuration': '型号 技术参数 | BBS-V600\n外形尺寸 | 600×580×1060mm',
    'configuration_url': OLAIBO_CONFIGURED,
    'configuration_source': 'product_page_spec',
    'detail_page': {'url': OLAIBO_CONFIGURED, 'status': 'fetched',
                    'origin': 'candidate_model_search', 'coverage': 'parameters'},
}


def _candidate(model, configuration, **overrides):
    candidate = {'model': model, 'brand': '', 'url': 'http://example.test/' + model,
                 'configuration': configuration}
    candidate.update(overrides)
    return candidate


# ---------------------------------------------------------------- §Q3a 撤掉没配置的候选

def test_candidates_without_configuration_are_withdrawn_from_both_rosters():
    """灭菌器那一轮：老肯 HSA-1500M 与倍力曼 MST-H 9-6-18 HS2 配置为空，申安留着。

    "撤掉"是**从展示名单撤掉**，不是搬进参考候选（设计规范对"降级成线索放在折叠区"
    这个选项的回答是「撤掉吧」）。2026-10-07 设计规范补了一条：参考候选（"未进入深入核验
    的其他线索"）里没读到配置的**同样不许展示**——同一张卡片上摆出来是同一个空配置栏。
    两份名单一起撤；**有配置的照旧展示**，参考候选那一区不是被整区删掉。
    """
    configured = _candidate('LDZH-100KBS', '型号 LDZH-100KBS\n容积 | 100升')
    empty_a = _candidate('HSA-1500M', '')
    empty_b = _candidate('MST-H 9-6-18 HS2', '')
    run = {'alternatives': [empty_a, configured],
           'reference_candidates': [empty_b, _candidate('BCV-3S1', '')],
           'discovered_candidates': [empty_a, empty_b, configured],
           'gaps': []}

    withdrawn = withdraw_unconfigured_candidates(run)

    assert [c['model'] for c in withdrawn] == ['HSA-1500M', 'MST-H 9-6-18 HS2', 'BCV-3S1']
    assert [c['model'] for c in run['alternatives']] == ['LDZH-100KBS']
    assert run['reference_candidates'] == []
    # "撤掉的是卡片，不是发现阶段的事实"：发现名单一个都不动，撤掉的候选还在里面。
    assert len(run['discovered_candidates']) == 3


def test_a_reference_candidate_with_a_configuration_stays():
    """参考候选那一区只收窄、不整区删：带配置的参考候选一条都不动。"""
    kept_lead = _candidate('BXM-170V', '型号 BXM-170V\n容积 | 170升')
    run = {'alternatives': [], 'reference_candidates': [kept_lead, _candidate('BXM-145V', '')],
           'discovered_candidates': [], 'gaps': []}

    assert [c['model'] for c in withdraw_unconfigured_candidates(run)] == ['BXM-145V']
    assert [c['model'] for c in run['reference_candidates']] == ['BXM-170V']


def test_withdrawn_candidates_leave_exactly_one_sentence_in_gaps():
    run = {'alternatives': [_candidate('HSA-1500M', '')],
           'reference_candidates': [], 'gaps': []}

    withdraw_unconfigured_candidates(run)
    withdraw_unconfigured_candidates(run)

    notes = [gap for gap in run['gaps'] if '找不到配置就不展示' in gap]
    assert len(notes) == 1
    assert 'HSA-1500M' in notes[0] and '没有被删除' in notes[0]


def test_blank_looking_configuration_is_not_a_configuration():
    """空白串与只有空格的串都算"没配置"——卡片上摆出来就是空的。"""
    run = {'alternatives': [_candidate('A-1', '   '), _candidate('A-2', '\n')],
           'reference_candidates': [], 'gaps': []}

    assert len(withdraw_unconfigured_candidates(run)) == 2
    assert run['alternatives'] == []


def test_a_card_whose_rows_carry_readings_is_not_withdrawn():
    """天平那一轮的华志 ``PTY-124/223``：配置文本为空，但参数矩阵里有读到的值。

    设计规范在那张卡片上读到的正是这些值（原话「其实是符合的」），所以它不是
    "没有配置的垃圾信息"——撤掉它等于把用户已经判过的东西拿走。
    """
    candidate = _candidate('PTY-124/223', '', matrix=[
        {'name': '可读性', 'observed': {'value': '120', 'unit': 'g'}, 'status': 'satisfied'},
    ])
    run = {'alternatives': [candidate], 'reference_candidates': [], 'gaps': []}

    assert withdraw_unconfigured_candidates(run) == []
    assert [c['model'] for c in run['alternatives']] == ['PTY-124/223']


def test_a_row_of_unknowns_is_not_a_configuration():
    """灭菌器那一轮的老肯 ``HSA-1500M``：8 行逐项比对，一个值都没读到。

    对齐表里整排 ``unknown`` 不是配置（卡上就是一片"未取得证据"），所以它撤。
    """
    items = [{'name': '指标%d' % index, 'observed': {'value': None},
              'observed_status': 'unknown'} for index in range(8)]
    candidate = _candidate('HSA-1500M', '', config_match={'items': items})
    assert candidate_has_configuration(candidate) is False

    run = {'alternatives': [candidate], 'reference_candidates': [], 'gaps': []}
    assert len(withdraw_unconfigured_candidates(run)) == 1


def test_a_config_match_reading_also_counts_as_configuration():
    candidate = _candidate('LS-50HV', '', config_match={
        'items': [{'name': '容积', 'observed': {'value': '50', 'unit': 'L'}}]})
    assert candidate_has_configuration(candidate) is True


# -------------------------------------------------- §Q6 展示链接 = 抽配置的那一页

def test_main_link_moves_to_the_page_the_configuration_came_from():
    candidate = dict(BEKOMAT)

    assert align_candidate_source_url(candidate) is True

    assert candidate['url'] == OLAIBO_CONFIGURED
    # 发现页地址一个字不丢：字段里留着，来源清单里也留着。
    assert candidate['discovery_url'] == OLAIBO_DISCOVERED
    pages = {row['url']: row for row in candidate['source_pages']}
    assert set(pages) == {OLAIBO_DISCOVERED, OLAIBO_CONFIGURED}
    assert pages[OLAIBO_CONFIGURED]['primary'] is True
    assert pages[OLAIBO_DISCOVERED]['primary'] is False
    # 配置本身与它的出处一格都没改。
    assert candidate['configuration'] == BEKOMAT['configuration']
    assert candidate['configuration_url'] == OLAIBO_CONFIGURED


def test_alignment_is_idempotent_and_leaves_an_aligned_candidate_alone():
    candidate = dict(BEKOMAT)
    align_candidate_source_url(candidate)
    before = dict(candidate)

    assert align_candidate_source_url(candidate) is False
    assert candidate == before


def test_candidate_already_pointing_at_the_configuration_page_is_untouched():
    """申安那一轮：候选的 url 与 configuration_url 本来就是同一页。"""
    candidate = {'model': 'LDZH-100KBS', 'url': 'https://miejunqi.com.cn/product/ldzh100kbs.html',
                 'configuration': '容积 | 100升',
                 'configuration_url': 'https://miejunqi.com.cn/product/ldzh100kbs.html#x',
                 'detail_page': {'url': 'https://miejunqi.com.cn/product/ldzh100kbs.html',
                                 'status': 'fetched', 'coverage': 'parameters'}}
    # 同一个地址的两种写法（锚点）不产生一次"搬家"。
    assert align_candidate_source_url(candidate) is False
    assert candidate['url'] == 'https://miejunqi.com.cn/product/ldzh100kbs.html'


def test_model_only_detail_page_is_not_a_configuration_page():
    """只写着型号的那一页不算配置出处，不能拿它当理由去改主链接。"""
    candidate = {'model': 'BCV-3S1', 'url': 'https://www.jiancaizhaobiao.cn/news-07fb',
                 'configuration': '',
                 'detail_page': {'url': 'https://beingyiqi.com/bcv-3s1.html',
                                 'status': 'fetched', 'coverage': 'model_only'}}
    assert align_candidate_source_url(candidate) is False
    assert candidate['url'] == 'https://www.jiancaizhaobiao.cn/news-07fb'


def test_verification_gate_still_recognises_the_kept_discovering_page():
    """链接搬家之后，引文所在的那一页照样过核验入口（判据没有放宽）。"""
    candidate = {'model': 'bekomat30', 'url': OLAIBO_CONFIGURED,
                 'discovery_url': OLAIBO_DISCOVERED,
                 'excerpt': 'bekomat30 洁净工作台'}
    sources = [{'url': OLAIBO_DISCOVERED, 'body_text': '洁净工作台BBS-H1300\nbekomat30 洁净工作台\n'}]

    assert candidate_verification_source(candidate, sources) is not None


def test_verification_gate_still_refuses_a_page_without_the_model():
    """同一条路：引文里认不出这个型号，照样返回 None。"""
    candidate = {'model': 'bekomat30', 'url': OLAIBO_CONFIGURED,
                 'discovery_url': OLAIBO_DISCOVERED,
                 'excerpt': 'BBS-H1300 洁净工作台'}
    sources = [{'url': OLAIBO_DISCOVERED, 'body_text': '洁净工作台BBS-H1300\n'}]

    assert candidate_verification_source(candidate, sources) is None


# ------------------------------------------------------- §Q2 拒收按来源聚合

def _rejection(url, model='HVE-50', code='model_mismatch', others=('HQL-150', 'SPX-150B')):
    return {'url': url, 'model': model, 'reason_code': code,
            'excerpt': '…%s…' % others[0], 'other_models': list(others),
            'detail': '引文中的型号是 %s，与本次目标型号 %s 不一致：同品类的不同型号不能互相认定成交价。'
                      % ('、'.join(others), model)}


#: 洁净台那一轮的真实来源地址（24 条金额全部来自它）。
CLEANBENCH_NOTICE = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260915_27329523.htm'


def test_one_notice_full_of_other_models_folds_into_a_single_record():
    records = [_rejection(CLEANBENCH_NOTICE) for _ in range(24)]

    kept, folded = aggregate_rejections_by_source(records)

    assert len(kept) == 1
    assert folded == 23
    assert kept[0]['aggregated_count'] == 24
    assert kept[0]['reason_code'] == 'model_mismatch'
    assert kept[0]['url'] == CLEANBENCH_NOTICE
    # 型号并集留着——那正是"这条公告讲的是别的仪器"的证据。
    assert kept[0]['other_models'] == ['HQL-150', 'SPX-150B']
    assert '24 条' in kept[0]['detail'] and '别的仪器' in kept[0]['detail']


def test_the_threshold_is_the_declared_one_and_two_are_left_alone():
    assert REJECTION_SOURCE_AGGREGATE_MIN == 3
    records = [_rejection(CLEANBENCH_NOTICE) for _ in range(REJECTION_SOURCE_AGGREGATE_MIN - 1)]

    kept, folded = aggregate_rejections_by_source(records)

    assert len(kept) == REJECTION_SOURCE_AGGREGATE_MIN - 1
    assert folded == 0


def test_a_source_that_refused_on_other_grounds_keeps_every_record():
    """只折叠"型号不符"。同一条来源上混着品牌不符时，逐条原样保留。"""
    records = ([_rejection(CLEANBENCH_NOTICE) for _ in range(5)]
               + [_rejection(CLEANBENCH_NOTICE, code='brand_mismatch')])

    kept, folded = aggregate_rejections_by_source(records)

    assert len(kept) == 6 and folded == 0


def test_records_from_different_sources_are_never_mixed():
    records = [_rejection(CLEANBENCH_NOTICE) for _ in range(4)] + \
              [_rejection('http://other.test/notice-1') for _ in range(4)]

    kept, folded = aggregate_rejections_by_source(records)

    assert len(kept) == 2 and folded == 6
    assert {row['url'] for row in kept} == {CLEANBENCH_NOTICE, 'http://other.test/notice-1'}


def test_aggregation_folds_nothing_when_there_is_nothing_to_fold():
    kept, folded = aggregate_rejections_by_source([])
    assert kept == [] and folded == 0


# ------------------------------------------------- §规范项 疑似同一笔的标注

#: b3-r8-median 那 9 条里的两对：同额同日、来源不同站。
PAIRS = [
    (48390.00000000001, '2025-09-15', 'http://zbcg.wit.edu.cn/jgggwtcg/2126.chtml'),
    (48390.00000000001, '2025-09-15',
     'http://ccgp-hubei.gov.cn/notice/202509/notice_b606489d116d4eb98bd5406029ffb6b7.html'),
    (63000.0, '2024-05-11', 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202405/t20240511_22076210.htm'),
    (63000.0, '2024-05-11', 'https://www.zsjypt.cn/artical/55/234703'),
]


def _price(amount, date, url):
    return {'model': 'HVE-50', 'unit_price': amount, 'date': date, 'url': url,
            'amount': amount, 'quantity': 1, 'kind': 'award'}


def test_both_measured_pairs_are_flagged_on_every_row_involved():
    rows = [_price(*item) for item in PAIRS]

    assert annotate_suspected_same_transaction(rows) == 4

    for row in rows:
        note = row['suspected_same_transaction']
        assert note['reason'] == SUSPECTED_SAME_TRANSACTION_REASON
        assert note['date'] == row['date']
        assert note['unit_price'] == round(row['unit_price'], 2)
        assert [item['url'] for item in note['counterparts']] != []
        assert row['url'] not in [item['url'] for item in note['counterparts']]


def test_rows_differing_in_amount_date_or_host_are_not_flagged():
    rows = [
        _price(48390.0, '2025-09-15', 'http://a.test/1'),
        _price(48390.0, '2025-09-16', 'http://b.test/2'),   # 日期不同
        _price(48391.0, '2025-09-15', 'http://c.test/3'),   # 金额不同
        _price(48390.0, 'unknown', 'http://d.test/4'),      # 日期没写
    ]

    assert annotate_suspected_same_transaction(rows) == 0
    assert all('suspected_same_transaction' not in row for row in rows)


def test_the_same_page_twice_is_not_a_suspected_pair():
    """同一个地址的两次读数是 ``_same_transaction`` 的事，不是"疑似同一笔"。"""
    rows = [_price(48390.0, '2025-09-15', 'http://a.test/1'),
            _price(48390.0, '2025-09-15', 'http://a.test/1')]

    assert annotate_suspected_same_transaction(rows) == 0


def test_the_note_never_changes_the_adopted_price_set():
    """标注只加字段：笔数、金额、日期一条都不动（``_merge_price_rows`` 的出口）。"""
    rows = [_price(*item) for item in PAIRS] + [_price(52000.0, '2022-04-27', 'https://bid.10huan.com/x')]

    merged = _merge_price_rows([dict(row) for row in rows])

    assert len(merged) == 5
    assert sorted(row['unit_price'] for row in merged) == sorted(row['unit_price'] for row in rows)
    # 唯一多的东西就是那句标注，且只有参与那两对的 4 条带着它。
    flagged = [row for row in merged if 'suspected_same_transaction' in row]
    assert len(flagged) == 4


def test_a_lone_row_keeps_its_exact_shape():
    """单独一条价格行不许被加上任何字段（下游有逐字比较整行的断言）。"""
    row = _price(52000.0, '2022-04-27', 'https://bid.10huan.com/x')
    assert _merge_price_rows([row]) == [row]
    assert _merge_price_rows([None, 'x']) == []
