# -*- coding: utf-8 -*-
"""相似品候选的查价留痕：三种「没有价」必须长得不一样（`设计规范` 的方案 C）。

`设计规范` 查清的结论是两条路**共用同一条抽价链**（同一个 ``prices()``、同一套三个
入口判据、同一个 ``validate_price``、同一套按型号分键的拒绝留痕），不一样的只是
"谁去把公告抓进来"。所以这一单不改通道、不改判据，只让用户看得出一个相似品候选
没有价是

* ① **没查**（这一轮压根没跑抽价），
* ② **查了没有**（来源有了，正文里没有它的成交单价），
* ③ **有来源但没被采信**（抽到了候选行，被采信判据挡下）。

三种情况各自的判据与数字口径都在 ``backend/services/alternative_price_lookup.py``；
下面既钉纯函数的分支，也走一遍真的 ``alternatives()``（模型与检索都是离线替身，
一条网络请求都不发）。
"""
import json

from backend.services.alternative_price_lookup import (
    EDUCATION_PLATFORM, NO_PRICE_SOURCE, NOT_ATTEMPTED, NOT_ATTEMPTED_REASONS, OTHER_CHANNEL,
    PRICED, PROVINCE_AWARD, PROVINCE_TEXT_METHOD, READ_WITHOUT_PRICE, REFUSED,
    RETAINED_FROM_PREVIOUS_ROUND, SOURCES_NOT_READ, STATUSES, UNRECORDED,
    alternative_lookup, candidate_lookup, channel_counts, not_attempted, price_lookup,
    scan_record, source_channel)
from backend.services.price_rejections import (
    SKIP_MODEL_NOT_IN_BODY, SKIP_NO_AMOUNT_EVIDENCE, SKIP_SOURCE_LIMIT,
    SKIP_TYPE_NOT_PRICE_SOURCE, explain_rejection, near_miss_rejections)
from backend.services.procurement_research import ON_DEMAND_ENV
from backend.services.procurement_rules import MODEL_MISMATCH
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
#: 公告逐行印字段：型号行上面一行就是那条成交单价（``price_regions`` 的窗口）。
NOTICE_BODY = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
               '采购人：某某医院\n'
               '生物安全柜 乙厂 Q99 数量1台 单价：56,000.00 元\n'
               '生物安全柜 乙厂 Q20 数量1台\n')
NOTICE_BODY_PRICED = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                      '采购人：某某医院\n'
                      '生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元\n')
NOTICE_BODY_NO_AMOUNT = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                         '采购人：某某医院\n'
                         '生物安全柜 乙厂 Q20 数量1台\n')
NOTICE_URL = 'https://hospital-a.example.com/notice/2026-09-11'
#: 第二条来源（过了入口、真的被读过的那个）——用来把「有来源但被拒」(③) 与
#: 「没有价格来源」(②) 分开：``eligible == 0`` 时不可能有 ③。
READ_URL = 'https://hospital-b.example.com/notice/2026-09-12'
CANDIDATE_EXCERPT = '生物安全柜 乙厂 Q20 数量1台'
#: 一份**别的型号**的成交公告。正文里没有目标型号，所以入口那条"正文须逐字含目标型号"
#: 判据（``price_rejections.py`` 的 ``contains_model``，**本单一个字都没动**）会把它挡在
#: 抽价循环之外；而 ``near_miss_rejections()`` 仍会从它里面抽出行、给同一个 target 写
#: 一条 ``model_mismatch`` **展示条目**。这正是录屏轮那条误导的来源（正文行用 ``|``
#: 分列、金额带单位——两个条件都是 ``near_miss_rejections`` 抽行所必需的形状）。
SIBLING_NOTICE_BODY = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                       '采购人：某某医院\n'
                       '1-1 | 生物安全柜 | 生物安全柜 | 乙厂 | H1850R | 1.00(台) | 32,980.00 元 | 32,980.00 元\n')
SIBLING_MODEL = 'H1850R'
SIBLING_TARGET_MODEL = 'H1850'
#: 候选**自己**的那条来源：它逐字写着候选型号（所以这条候选进得了名单），但它不是价格
#: 来源类型（``source_type='unknown'``）→ 被入口第二判据挡下。与上面那条公告一起，
#: 就凑出「来源都看到了、一条都没进抽价循环」的形状。
NAMES_CANDIDATE_SOURCE = {
    'url': 'https://shop.example.com/H1850', 'purpose': 'discovery', 'body_status': 'fetched',
    'source_type': 'unknown',
    'body_text': '乙厂 H1850 生物安全柜 产品介绍 型号 H1850',
}


def notice(url=NOTICE_URL, body=NOTICE_BODY, source_type='tender_document'):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': source_type, 'title': '实验室设备采购项目中标（成交）结果公告',
            'body_text': body}


def _skip(model, reason, url):
    return {'url': url, 'host': 'a.gov.cn', 'source_type': 'tender_document',
            'body_status': 'fetched', 'body_len': 100, 'target_model': model,
            'skip_reason': reason}


#: 一行**真的**被核价判据拒掉的价格行所在的公告正文（型号 Q99 ≠ 目标 Q20）。
REFUSAL_BODY = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                '采购人：某某医院\n'
                '生物安全柜 乙厂 Q99 数量1台 单价：56,000.00 元\n')


def _display_rows(model='Q20'):
    """**真** ``near_miss_rejections()`` 产物：展示条目，从没进过核价那一步。

    故意跑真函数而不是手写字典——这样 ``display_only`` 标记一旦被谁删掉，
    依赖它的状态用例会跟着红。
    """
    return near_miss_rejections([notice(body=SIBLING_NOTICE_BODY)], {'brand': '乙厂', 'model': model})


def _real_refusal(model='Q20', url=READ_URL):
    """**真** ``explain_rejection()`` 产物：一条价格行确实被核价判据拒了。"""
    row = {'kind': 'award', 'url': url, 'brand': '乙厂', 'model': 'Q99', 'amount': 56000,
           'amount_scope': 'unit', 'currency': 'CNY', 'date': '2026-09-12',
           'excerpt': '生物安全柜 乙厂 Q99 数量1台 单价：56,000.00 元'}
    return explain_rejection(row, notice(url=url, body=REFUSAL_BODY),
                            {'brand': '乙厂', 'model': model})


# ---------------------------------------------------------------------------
# 纯函数：通道身份与三种情况的判据
# ---------------------------------------------------------------------------

def test_the_channel_comes_from_the_registry_not_from_a_domain_guess():
    """通道身份读注册表（教育平台的 ``PLATFORMS`` / 省级的 ``PROVINCES``），不猜域名。"""
    assert source_channel({'url': 'https://gdegp.gds.edu.cn/fpjgDetail.aspx?id=1'}) == EDUCATION_PLATFORM
    assert source_channel({'url': 'https://gdegp.gds.edu.cn:8093/x', 'platform': 'xlyg'}) == EDUCATION_PLATFORM
    assert source_channel({'url': 'https://www.ccgp-anhui.gov.cn/x', 'platform': 'anhui'}) == PROVINCE_AWARD
    assert source_channel({'url': 'https://www.ccgp-jiangxi.gov.cn/x',
                           'text_provenance': {'method': PROVINCE_TEXT_METHOD}}) == PROVINCE_AWARD
    # 认不出来的一律 other：检索句抓回来的公告、附件、厂家资料页都在这一档。
    assert source_channel({'url': 'https://www.ccgp.gov.cn/x'}) == OTHER_CHANNEL
    assert source_channel(None) == OTHER_CHANNEL
    assert channel_counts([{'url': 'https://gdegp.gds.edu.cn/a'},
                           {'platform': 'anhui'}, {}, 'not-a-dict']) == {
        EDUCATION_PLATFORM: 1, PROVINCE_AWARD: 1, OTHER_CHANNEL: 1}   # 非字典不算来源


def test_the_scan_record_counts_sources_not_requests():
    record = scan_record([{'url': 'https://a.gov.cn/1'}, {'url': 'https://b.gov.cn/2', 'platform': 'anhui'}])
    assert record == {'sources_scanned': 2,
                      'sources_by_channel': {EDUCATION_PLATFORM: 0, PROVINCE_AWARD: 1, OTHER_CHANNEL: 1}}


def test_never_looked_is_its_own_record_with_a_reason():
    """① 没查：这是方案 C 补上的那一环——过去一行留痕都没有。"""
    assert not_attempted(RETAINED_FROM_PREVIOUS_ROUND, [{'a': 1}]) == {
        'status': NOT_ATTEMPTED, 'reason_code': RETAINED_FROM_PREVIOUS_ROUND, 'price_count': 1}
    assert NOT_ATTEMPTED in STATUSES and RETAINED_FROM_PREVIOUS_ROUND in NOT_ATTEMPTED_REASONS


def test_a_candidate_without_a_model_is_not_a_card_and_is_not_recorded():
    """没有型号的候选不是卡片：调用点与既有的 ``continue`` 共用条件，所以要容错。"""
    from backend.services.procurement_research import ProcurementResearch
    candidate = {'brand': '乙厂'}
    assert ProcurementResearch.note_price_lookup_not_attempted(candidate, RETAINED_FROM_PREVIOUS_ROUND) is None
    assert 'price_lookup' not in candidate


def test_an_empty_scan_is_not_attempted_rather_than_looked_and_found_nothing():
    """抽价跑了但一条来源都没有：这就是「这一轮根本没给它找来源」。"""
    lookup = price_lookup({'sources': []}, 'Q20', [], scan_record([]))
    assert lookup == {'status': NOT_ATTEMPTED, 'reason_code': 'no_sources_found', 'price_count': 0}


def test_looked_and_found_nothing_has_three_distinguishable_shapes():
    """② 查了没有：来源没进通道 / 进了通道没被读 / 读了没抽到——三个不同的数。"""
    run = {'sources': [], 'price_source_skips': [
        _skip('Q20', SKIP_MODEL_NOT_IN_BODY, 'https://a.gov.cn/1'),
        _skip('Q20', SKIP_TYPE_NOT_PRICE_SOURCE, 'https://b.com/2'),
    ], 'price_rejections': []}
    scan = scan_record([{'url': 'https://a.gov.cn/1'}, {'url': 'https://b.com/2'}])

    lookup = price_lookup(run, 'Q20', [], scan)
    assert lookup['status'] == NO_PRICE_SOURCE
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (2, 2, 0, 0)
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_TYPE_NOT_PRICE_SOURCE, 'count': 1},
                                      {'skip_reason': SKIP_MODEL_NOT_IN_BODY, 'count': 1}]

    # 同一批来源里有一条过了入口，但被成本门挡下：读都没读，是可重试的那一种。
    run['price_source_skips'].append(_skip('Q20', SKIP_NO_AMOUNT_EVIDENCE, 'https://c.gov.cn/3'))
    scan = scan_record([{'url': 'https://a.gov.cn/1'}, {'url': 'https://b.com/2'},
                        {'url': 'https://c.gov.cn/3'}])
    lookup = price_lookup(run, 'Q20', [], scan)
    assert lookup['status'] == SOURCES_NOT_READ
    assert (lookup['sources_scanned'], lookup['sources_eligible'], lookup['sources_cost_gated']) == (3, 1, 1)

    # 被读过的来源不会留 skip：有来源进了通道、成本门也没挡它，就是读了没抽到价。
    run['price_source_skips'] = [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, 'https://a.gov.cn/1')]
    scan = scan_record([{'url': 'https://a.gov.cn/1'}, {'url': 'https://d.gov.cn/4'}])
    lookup = price_lookup(run, 'Q20', [], scan)
    assert lookup['status'] == READ_WITHOUT_PRICE
    assert (lookup['sources_scanned'], lookup['sources_eligible'], lookup['sources_cost_gated']) == (2, 1, 0)
    assert lookup['price_count'] == 0


def test_repeated_cost_gate_records_count_sources_not_records():
    """成本门留痕每个 pass 追加一行、run 级不去重：卡片上不许虚报成"8 条来源"。

    ``prices()`` 一个候选要跑很多次（先一次、每条采购方检索句之后各一次），而
    ``price_source_skips`` 里成本门那两行不会像入口那三行一样被下一次 pass 覆盖。
    """
    run = {'price_source_skips': [_skip('Q20', SKIP_SOURCE_LIMIT, NOTICE_URL) for _ in range(8)],
           'price_rejections': []}
    lookup = price_lookup(run, 'Q20', [], scan_record([{'url': NOTICE_URL}]))
    assert lookup['sources_cost_gated'] == 1
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_SOURCE_LIMIT, 'count': 1}]
    assert lookup['status'] == SOURCES_NOT_READ


def test_candidate_lookup_writes_the_card_fields_and_drops_the_scratch_scan():
    """``candidate_lookup`` 把结论与逐条留痕写到卡片上，中间账 ``price_scan`` 删掉。

    这里要的是 ③：除被入口挡下的那条之外再给一条**没被挡**的来源，``eligible`` 才是
    1（``eligible == 0`` 那一档由
    ``test_a_near_miss_display_row_does_not_turn_an_empty_price_lane_into_a_refusal``
    单独钉——那时状态必须是 ②，不是 ③）。
    """
    run = {'sources': [],
           'price_source_skips': [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, NOTICE_URL)],
           'price_rejections': [{'url': READ_URL, 'model': 'Q20',
                                 'reason_code': MODEL_MISMATCH}]}
    candidate = {'model': 'Q20', 'prices': [],
                 'price_scan': scan_record([{'url': NOTICE_URL}, {'url': READ_URL}])}

    lookup = candidate_lookup(run, candidate)

    assert lookup['status'] == REFUSED and lookup['price_count'] == 0
    assert lookup['sources_eligible'] == 1
    assert 'price_scan' not in candidate
    assert [record['target_model'] for record in candidate['price_source_skips']] == ['Q20']
    assert [record['reason_code'] for record in candidate['price_rejections']] == [MODEL_MISMATCH]


def test_a_near_miss_display_row_does_not_turn_an_empty_price_lane_into_a_refusal():
    """口径：``eligible == 0`` 时状态是 ②「没有价格来源」，不是 ③「有来源但被拒」。

    复现录屏轮那条误导（`设计规范` §3.4；追查见
    ``output/price-reject-audit/report.md`` §0.3）：候选 JC101 名下
    ``sources_scanned=48 / sources_entry_dropped=48 / sources_eligible=0``，而同一次
    ``prices()`` 里 ``near_miss_rejections()`` 给同一个 target 写了 12 条
    ``model_mismatch`` **展示条目**。旧的判定顺序先看"拒收集合非空"，于是"这一轮根本
    没有价格来源"被报成"抽到了价格行、被核价判据拒了"——对外粒度比内部事实粗一级。
    """
    display = _display_rows()
    run = {'price_source_skips': [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, NOTICE_URL)],
           'price_rejections': display}
    lookup = price_lookup(run, 'Q20', [], scan_record([{'url': NOTICE_URL}]))

    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible']) == (1, 1, 0)
    assert lookup['status'] == NO_PRICE_SOURCE
    assert lookup['price_count'] == 0
    # 展示条目一条都不许删：状态变细不等于把"跳过"和"丢数据"混成一句
    # （根 AGENTS.md「对外口径与兜底」第 3 条）。
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH, 'count': len(display)}]


def test_only_the_display_entries_carry_the_display_only_marker():
    """标记本身：只有展示条目带 ``display_only``，真实拒收记录一个都不许带。

    标记是"这两类记录必须分得开"的唯一依据，所以单独钉一条；三条状态用例只管状态，
    回退标记时它们会以 ``assert 'refused' == ...`` 的形式红。
    """
    display = _display_rows()
    assert display, '前提：别的型号的公告必须产出展示条目'
    assert all(record.get('display_only') is True for record in display)
    real = _real_refusal()
    assert 'display_only' not in real, '真实拒收记录不许带展示条目的标记'


def test_only_display_entries_and_a_cost_gated_source_is_not_read_not_no_source():
    """场景 A（设计规范定的三条之一）：拒收全是展示条目 ⇒ ② `sources_not_read`。

    ``eligible == 1``（确实有一条来源写着这个型号、过了入口）但被成本门挡下、一条没读，
    同时另一份**别的型号**的公告留下展示条目。真实内部事实是"有来源、没读到正文"，
    对外就该这么说——既不是"压根没有价格来源"，更不是"抽到的价格行被拒"。
    这条正是追查报告里的 reverify-a（`eligible=1`、`cost_gated=1`）。
    """
    run = {'price_source_skips': [_skip('Q20', SKIP_NO_AMOUNT_EVIDENCE, READ_URL)],
           'price_rejections': _display_rows()}
    lookup = price_lookup(run, 'Q20', [], scan_record([{'url': READ_URL}]))

    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (1, 0, 1, 1)
    assert lookup['status'] == SOURCES_NOT_READ
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH,
                                            'count': len(_display_rows())}]


def test_a_source_that_was_read_with_only_display_entries_is_read_without_price():
    """场景 B：读过来源、没抽出价 ⇒ ② `read_without_price`，不是 ③、也不是"没来源"。

    一条别的型号的公告被入口挡下（并留下展示条目），另一条写着目标型号的公告真的被读了
    却一行价都没抽到。真实内部事实是"读了、正文里没有它的成交单价"。
    """
    display = _display_rows()
    run = {'price_source_skips': [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, NOTICE_URL)],
           'price_rejections': display}
    scan = scan_record([{'url': NOTICE_URL}, {'url': READ_URL}])
    lookup = price_lookup(run, 'Q20', [], scan)

    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (2, 1, 1, 0)
    assert lookup['status'] == READ_WITHOUT_PRICE
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH, 'count': len(display)}]


def test_a_real_refusal_with_an_eligible_source_is_still_case_three():
    """场景 C（防回归）：只要有一条**真被核价判据拒掉**的价格行，状态就是 ③。

    ``refused_rows`` 排在成本门计数之前是刻意的（``sources_cost_gated`` 跨 pass 累积、
    而 ``sources_eligible`` 只是最后一次 pass 的账），所以这里再叠一条成本门记录，状态
    仍然必须是 ③——展示条目混在里面也不改变它。
    """
    real = _real_refusal()
    assert real['reason_code'] == MODEL_MISMATCH
    display = _display_rows()
    run = {'price_source_skips': [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, READ_URL),
                                  _skip('Q20', SKIP_SOURCE_LIMIT, READ_URL)],
           'price_rejections': [real] + display}
    scan = scan_record([{'url': NOTICE_URL}, {'url': READ_URL}])
    lookup = price_lookup(run, 'Q20', [], scan)

    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (2, 1, 1, 1)
    assert lookup['status'] == REFUSED
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH,
                                            'count': 1 + len(display)}]


def test_a_refused_row_is_case_three_and_a_price_wins_over_it():
    """③ 有来源但没被采信：行被抽到了、被拒了；同时有价时"有价"优先。"""
    run = {'price_source_skips': [], 'price_rejections': [
        {'url': NOTICE_URL, 'model': 'Q20', 'reason_code': MODEL_MISMATCH, 'detail': '型号不一致'},
    ]}
    scan = scan_record([{'url': NOTICE_URL}])
    lookup = price_lookup(run, 'Q20', [], scan)
    assert lookup['status'] == REFUSED
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH, 'count': 1}]
    assert price_lookup(run, 'Q20', [{'unit_price': 56000}], scan)['status'] == PRICED
    assert price_lookup(run, 'Q20', [{'unit_price': 56000}], scan)['price_count'] == 1


def test_the_records_are_keyed_by_target_model_and_do_not_mix():
    """留痕按型号分键：另一个候选的记录不许出现在这张卡片上。

    ``Q99`` 那半边的状态是 ②（它名下唯一的来源被入口挡下、``eligible == 0``），不是 ③
    ——那条 ``model_mismatch`` 因此只能算展示条目。这里要钉的是**记录没串**：Q20 的
    ``rejection_reasons`` 为空、Q99 的那条落在 Q99 上。
    """
    run = {'price_source_skips': [_skip('Q20', SKIP_MODEL_NOT_IN_BODY, 'https://a.gov.cn/1'),
                                  _skip('Q99', SKIP_MODEL_NOT_IN_BODY, 'https://a.gov.cn/1')],
           'price_rejections': [{'url': NOTICE_URL, 'model': 'Q99', 'reason_code': MODEL_MISMATCH}]}
    scan = scan_record([{'url': 'https://a.gov.cn/1'}])
    assert price_lookup(run, 'Q20', [], scan)['status'] == NO_PRICE_SOURCE
    assert price_lookup(run, 'Q20', [], scan)['rejection_reasons'] == []
    assert price_lookup(run, 'Q99', [], scan)['status'] == NO_PRICE_SOURCE
    assert price_lookup(run, 'Q99', [], scan)['rejection_reasons'] == [
        {'reason_code': MODEL_MISMATCH, 'count': 1}]


def test_a_replaced_pricing_call_is_reported_as_unrecorded():
    """抽价被替身接管（测试/集成替身）时没有扫描账，不许在下结论时充数。"""
    assert price_lookup({}, 'Q20', [], None)['status'] == UNRECORDED
    assert price_lookup({}, 'Q20', [], None)['reason_code'] == 'scan_not_recorded'


def test_the_run_level_ledger_counts_candidates_and_channels():
    run = {'sources': [{'url': 'https://gdegp.gds.edu.cn/a'}, {'platform': 'anhui'},
                       {'url': 'https://a.gov.cn/1'}],
           'alternatives': [
               {'model': 'Q20', 'prices': [], 'price_lookup': {'status': READ_WITHOUT_PRICE,
                                                               'reason_code': None}},
               {'model': 'Q30', 'prices': [{'unit_price': 1}],
                'price_lookup': {'status': PRICED, 'reason_code': None}},
               {'model': 'Q40', 'prices': [], 'price_lookup': {'status': NOT_ATTEMPTED,
                                                               'reason_code': 'retained_from_previous_round'}}]}
    ledger = alternative_lookup(run)
    assert ledger['sources_scanned'] == 3
    assert ledger['sources_by_channel'] == {EDUCATION_PLATFORM: 1, PROVINCE_AWARD: 1, OTHER_CHANNEL: 1}
    assert ledger['candidates_scanned'] == 2 and ledger['candidates_with_price'] == 1
    assert [row['model'] for row in ledger['candidates']] == ['Q20', 'Q30', 'Q40']


# ---------------------------------------------------------------------------
# 端到端：真的走一遍 alternatives()，钉住三种情况落到卡片上的形状
# ---------------------------------------------------------------------------

def _batch_pricing_path(monkeypatch):
    """把"研究阶段批量查价"这条老路显式打开（``ALTERNATIVE_PRICE_ON_DEMAND=0``）。

    本文件钉的是**批量查价那一次扫描的留痕**：三种"没有价"的形状、成本门、逐条拒绝、
    上一轮沿用的卡片。这一段只在开关关着时跑（开关打开时 ``alternatives()`` 不抽价，
    每个候选只写"等用户点"）。按需那条路走的是同一个 ``verify_and_price_candidate``，
    它的留痕由 ``test_alternative_price_on_demand.py`` 覆盖。

    为什么是显式关掉开关、而不是把断言改成"没查"：``=0`` 是排障时的回退路径，
    它必须继续被钉住；改成"没查"等于把这些留痕的覆盖整段丢掉。
    """
    monkeypatch.setenv(ON_DEMAND_ENV, '0')


def _prepare(run, sources):
    # ``persist()`` 会读 ``baseline['version']`` 写回任务，所以基准是合并不是替换。
    run.update({'baseline': {**(run.get('baseline') or {}), **BASELINE},
                'sources': list(sources), 'alternatives': [],
                'discovered_candidates': [], 'reference_candidates': [],
                'prices': [], 'price_rejections': [], 'price_source_skips': [], 'gaps': []})


def _stub(service, monkeypatch, prices=(), candidates=None, persist=True):
    """模型与检索都换成离线替身；``prices`` 是抽价那一次模型返回的行。

    返回被问过的 prompt 列表——用它数"这一轮到底被读了几次"，而不是只看状态名。
    """
    calls = []

    def fake_model(run_, prompt, data):
        calls.append({'prompt': prompt, 'data': data})
        if prompt.startswith('从给定正文发现'):
            return {'candidates': list(candidates or [])}
        if '只提取正文中属于指定品牌' in prompt:
            return {'prices': [dict(row) for row in prices]}
        return {'parameters': [], 'queries': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    if not persist:
        monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    return calls


def _extractions(calls):
    """抽取价格那一次模型调用（prompt 与载荷一起，用来证明正文真的被读了）。"""
    return [call for call in calls if '只提取正文中属于指定品牌' in call['prompt']]


def _q20_candidate():
    #: ``configuration`` 不能空：2026-10-06 起"找不到配置的候选不展示"
    #: （``tests/test_m4_pipeline.py``），空配置的候选会在 ``alternatives()`` 收尾时被撤下
    #: 候选卡片，而这些用例读的正是卡片。取引文本身，保证过 ``supported``。
    return {'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': CANDIDATE_EXCERPT,
            'configuration': CANDIDATE_EXCERPT}


def _row(model, excerpt):
    return {'brand': '乙厂', 'model': model, 'kind': 'award', 'amount': 56000,
            'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'currency': 'CNY',
            'date': '2026-09-11', 'excerpt': excerpt}


def test_a_candidate_whose_sources_were_read_but_priced_nothing(research, monkeypatch):  # noqa: F811
    """② 最里面那一层：三条入口都过了、成本门也没挡，读了就是没抽出价。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [BROCHURE, notice(body=NOTICE_BODY_PRICED)])
    calls = _stub(service, monkeypatch, prices=[], candidates=[_q20_candidate()], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    lookup = card['price_lookup']
    assert lookup['status'] == READ_WITHOUT_PRICE
    # "读过了"不是从状态名推的：抽取那次模型调用确实发生过，而且载荷里带着 Q20 的公告正文。
    assert _extractions(calls), '这一轮必须至少被读过一次，否则状态该是"没读"'
    assert all('Q20' in json.dumps(call['data'], ensure_ascii=False) for call in _extractions(calls))
    # BROCHURE 正文里没有 Q20（入口挡）、公告逐字写着 Q20 且带金额（被读了一次）。
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (2, 1, 1, 0)
    assert lookup['sources_by_channel'] == {EDUCATION_PLATFORM: 0, PROVINCE_AWARD: 0, OTHER_CHANNEL: 2}
    assert card['prices'] == [] and lookup['price_count'] == 0
    # 逐条留痕也暴露到卡片上，且只属于这个型号。
    assert [record['skip_reason'] for record in card['price_source_skips']] == [SKIP_MODEL_NOT_IN_BODY]
    assert all(record['target_model'] == 'Q20' for record in card['price_source_skips'])
    assert card['price_rejections'] == []
    assert run['alternative_price_lookup']['candidates_with_price'] == 0


def test_a_candidate_whose_only_source_has_no_amount_is_not_read(research, monkeypatch):  # noqa: F811
    """② 中间那一层：来源写着它的型号，但正文里没有金额证据，一条都没读。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [notice(body=NOTICE_BODY_NO_AMOUNT)])
    _stub(service, monkeypatch, prices=[], candidates=[_q20_candidate()], persist=False)

    service.alternatives(run)

    lookup = run['alternatives'][0]['price_lookup']
    assert lookup['status'] == SOURCES_NOT_READ
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (1, 0, 1, 1)
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_NO_AMOUNT_EVIDENCE, 'count': 1}]


def test_sources_over_the_per_target_limit_are_the_same_case_two_not_a_refusal(research, monkeypatch):  # noqa: F811
    """② 成本门的另一半：5 条合格来源、每 target 只读 4 条，第 5 条是被额度挡下的。

    它是"没读到"，不是"读了被拒"——所以状态仍是 ②，而 ``price_source_limit`` 这条
    留痕会出现在卡片上（``设计规范`` 之前，这一条只有 run 级记录，卡片上看不见）。
    """
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    sources = [notice(url=f'https://hospital-{index}.example.com/notice',
                      body=NOTICE_BODY_PRICED + f'第{index}包\n') for index in range(5)]
    _prepare(run, sources)
    candidate = {**_q20_candidate(), 'url': sources[0]['url']}
    _stub(service, monkeypatch, prices=[], candidates=[candidate], persist=False)

    service.alternatives(run)

    lookup = run['alternatives'][0]['price_lookup']
    assert lookup['status'] == READ_WITHOUT_PRICE       # 读了 4 条，一条价都没抽到
    # run 级那份留痕**每个 pass 追加一行**（这里实测 8 行 = 8 次 ``prices()``），
    # 本单没动它；卡片上按来源去重后只报 1 条。
    raw = [record for record in run['price_source_skips']
           if record.get('skip_reason') == SKIP_SOURCE_LIMIT]
    assert {record['url'] for record in raw} == {sources[4]['url']}
    assert len(raw) > 1
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible'], lookup['sources_cost_gated']) == (5, 0, 5, 1)
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_SOURCE_LIMIT, 'count': 1}]


def test_a_candidate_with_a_source_of_the_wrong_type_is_case_two_not_three(research, monkeypatch):  # noqa: F811
    """② 最外面那一层：发现它的那页不是价格来源，入口三判据一条都没过。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [notice(source_type='unknown')])
    _stub(service, monkeypatch, prices=[], candidates=[_q20_candidate()], persist=False)

    service.alternatives(run)

    lookup = run['alternatives'][0]['price_lookup']
    assert lookup['status'] == NO_PRICE_SOURCE
    assert lookup['sources_eligible'] == 0
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_TYPE_NOT_PRICE_SOURCE, 'count': 1}]
    assert run['alternatives'][0]['price_rejections'] == []


def test_a_candidate_with_no_eligible_source_says_no_price_source_not_refused(research, monkeypatch):  # noqa: F811
    """录屏轮的复现：**没有价格来源**必须是 ②，哪怕展示条目让 ``price_rejections`` 非空。

    候选 ``H1850`` 这一轮看到两条来源、一条都没进抽价循环：写着自己型号的那页不是价格
    来源类型（类型判据挡），写着 **H1850R** 的那份成交公告正文里没有它（``contains_model``
    认为后缀字母不同就是不同型号——判据**一个字没放宽**，见
    ``test_model_match_signal.py``）。``eligible == 0`` 时抽价循环一次都没进（抽取那次
    模型调用不该发生）；可 ``near_miss_rejections()`` 仍给这个 target 写了一条
    ``model_mismatch`` 展示条目。旧顺序把这一档报成 ③「抽到了价格行没通过核价判据」，
    设计规范看到的就是「查到了但没有采用」。现在必须是 ②「没有价格来源」，而展示条目
    照旧留着（不删、也不并进价格）——"跳过"与"丢数据"分开写。
    """
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [NAMES_CANDIDATE_SOURCE, notice(body=SIBLING_NOTICE_BODY)])
    candidate = {'brand': '乙厂', 'model': SIBLING_TARGET_MODEL,
                 'url': NAMES_CANDIDATE_SOURCE['url'], 'excerpt': '乙厂 H1850 生物安全柜',
                 # 同上：空配置的候选会被新的展示口径撤下候选卡片。
                 'configuration': '乙厂 H1850 生物安全柜'}
    calls = _stub(service, monkeypatch, prices=[], candidates=[candidate], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    lookup = card['price_lookup']
    assert lookup['status'] == NO_PRICE_SOURCE
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible']) == (2, 2, 0)
    assert lookup['skip_reasons'] == [{'skip_reason': SKIP_TYPE_NOT_PRICE_SOURCE, 'count': 1},
                                      {'skip_reason': SKIP_MODEL_NOT_IN_BODY, 'count': 1}]
    # 展示条目（别的型号的行）还在，但它不再决定状态。
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH, 'count': 1}]
    assert [record['reason_code'] for record in card['price_rejections']] == [MODEL_MISMATCH]
    assert SIBLING_MODEL in card['price_rejections'][0]['detail']
    assert card['prices'] == []
    assert not _extractions(calls), '一条来源都没过入口，抽取那一次模型调用就不该发生'
    assert run['alternative_price_lookup']['candidates_with_price'] == 0


def test_a_row_that_was_extracted_and_refused_is_case_three(research, monkeypatch):  # noqa: F811
    """③ 有来源但没被采信：公告里那行是别的型号，抽到了也要被 ``validate_price`` 拒。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [notice()])
    other_model_line = NOTICE_BODY.splitlines()[2]
    _stub(service, monkeypatch, candidates=[_q20_candidate()],
          prices=[_row('Q99', other_model_line)], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    lookup = card['price_lookup']
    assert lookup['status'] == REFUSED
    assert card['prices'] == []          # 被拒的行一个字都不许进价格
    assert lookup['price_count'] == 0
    assert lookup['rejection_reasons'] == [{'reason_code': MODEL_MISMATCH, 'count': 1}]
    assert [record['reason_code'] for record in card['price_rejections']] == [MODEL_MISMATCH]
    # 同一批来源里没有别的被拒行，逐条留痕只属于这个型号。
    assert all(record['model'] == 'Q20' for record in card['price_rejections'])
    # 采信链的隔离规则没被动过：相似品的价不进 run['prices']。
    assert run['prices'] == []


def test_a_candidate_that_is_priced_says_priced(research, monkeypatch):  # noqa: F811
    """有价的那一档：状态是 ``priced``，而且这个价仍然只落在候选自己身上。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [notice(body=NOTICE_BODY_PRICED)])
    _stub(service, monkeypatch, candidates=[_q20_candidate()],
          prices=[_row('Q20', '生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元')], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    assert card['price_lookup']['status'] == PRICED
    assert card['price_lookup']['price_count'] == len(card['prices'])
    assert card['price_lookup']['price_count'] >= 1
    assert run['prices'] == []


def test_a_card_carried_over_from_the_last_round_says_this_round_did_not_look(research, monkeypatch):  # noqa: F811
    """① 没查：上一轮留下的卡片这一轮没重跑抽价——过去这种情况一个字都不留。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, _ = research
    _prepare(run, [notice(body=NOTICE_BODY_PRICED)])
    retained = {**_q20_candidate(), 'status': 'eligible', 'prices': [], 'matrix': []}
    run['alternatives'] = [retained]
    _stub(service, monkeypatch, candidates=[], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    assert card['price_lookup'] == {'status': NOT_ATTEMPTED,
                                    'reason_code': RETAINED_FROM_PREVIOUS_ROUND,
                                    'price_count': 0}
    assert run['alternative_price_lookup']['candidates_scanned'] == 0
    assert [row['status'] for row in run['alternative_price_lookup']['candidates']] == [NOT_ATTEMPTED]


def test_every_not_attempted_reason_is_recorded_with_its_own_code():
    """① 的六种原因码都记得下来（``alternatives()`` 里那几处 ``continue`` 各一个）。

    今天的选择规则（``diverse_candidates`` 先按品牌／型号／名额筛一遍）让其中几种
    事实上走不到，但留痕在这里逐个钉住：规则以后一改，"没查"不许再变成空白。
    """
    from backend.services.procurement_research import ProcurementResearch
    for reason in NOT_ATTEMPTED_REASONS:
        candidate = {'model': 'Q20'}
        record = ProcurementResearch.note_price_lookup_not_attempted(candidate, reason)
        assert record == {'status': NOT_ATTEMPTED, 'reason_code': reason, 'price_count': 0}


def test_the_run_ledger_and_the_api_carry_the_lookup_to_the_card(research, monkeypatch):  # noqa: F811
    """run 级总账与 API 透传：通道分档、扫描条数、卡片上的结论都要读得到。"""
    _batch_pricing_path(monkeypatch)
    service, run, _, client = research
    education = {'url': 'https://gdegp.gds.edu.cn:8093/Detail/1', 'platform': 'xlyg',
                 'source_type': 'unknown', 'body_status': 'fetched', 'purpose': 'tender',
                 'body_text': '小零易购 商品目录 生物安全柜'}
    province = {'url': 'https://www.ccgp-anhui.gov.cn/detail/1', 'platform': 'anhui',
                'source_type': 'tender_document', 'body_status': 'fetched', 'purpose': 'tender',
                'text_provenance': {'method': PROVINCE_TEXT_METHOD},
                'body_text': '安徽省本级 成交公告\n生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元'}
    _prepare(run, [BROCHURE, notice(body=NOTICE_BODY_PRICED), education, province])
    _stub(service, monkeypatch, prices=[], candidates=[_q20_candidate()])

    service.alternatives(run)

    ledger = run['alternative_price_lookup']
    assert ledger['sources_scanned'] == 4
    assert ledger['sources_by_channel'] == {EDUCATION_PLATFORM: 1, PROVINCE_AWARD: 1, OTHER_CHANNEL: 2}
    assert [row['status'] for row in ledger['candidates']] == [READ_WITHOUT_PRICE]
    # 教育平台那页是 unknown（入口类型挡）、BROCHURE 正文没有 Q20（型号挡）
    # —— 两条公告过了入口并带金额证据，都真的被读了一次。
    lookup = run['alternatives'][0]['price_lookup']
    assert (lookup['sources_scanned'], lookup['sources_entry_dropped'],
            lookup['sources_eligible']) == (4, 2, 2)

    # API 读回来的是 ``service.display`` 的那一份：新字段不需要动接口。
    body = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()
    assert body['alternative_price_lookup']['sources_by_channel'][EDUCATION_PLATFORM] == 1
    assert body['alternatives'][0]['price_lookup']['status'] == READ_WITHOUT_PRICE
    assert body['alternatives'][0]['price_lookup']['sources_eligible'] == 2
    assert [record['target_model'] for record in body['alternatives'][0]['price_source_skips']] \
        == ['Q20', 'Q20']
