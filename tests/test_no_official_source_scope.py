"""``no_official_source`` 是**每一次检索**的结论，不能当成**整个任务**的结论。

实测（``设计说明`` §3）：
* 这个告警挂在单次 ``SearchRun`` 上（``official_search.py:315-317``），
  判的是那一次检索返回的来源类型，判据里不含"任务层是否已有官方来源"；
* 一个任务能写二十多次：``task-4775bbb17607`` 20 次、``task-271ebe2c1de7`` 21 次、
  ``task-915675f4f23b`` 25 次；
* 而这三个 run **都有官方来源**——``task-4775bbb17607`` 唯一采信的价格就来自
  ``cgzx.hust.edu.cn``，报告却一边列着官方的价格、一边说"未找到已核实的官方来源"。

修法是：逐次检索的原始告警留在 ``run['search_attempts']`` 一个字不改，
只在**任务级缺口**上收敛成一句符合事实的话。
"""
import json

from backend.services.procurement_research import (
    _human_gap, is_no_official_source_gap, official_source_count,
    reconcile_official_source_gaps, report_blockers,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
NOTICE_BODY = ('中标（成交）结果公告\n项目编号：YXZC2026-G1-00082-HCZB-0013\n'
               '采购人：峨山彝族自治县医疗与健康服务集团\n'
               '成交供应商：惠而好医疗科技（云南）有限公司\n'
               '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480\n')


def _official():
    return {'url': NOTICE_URL, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': NOTICE_BODY, 'provider': 'ccgp'}


def _junk():
    return {'url': 'https://www.instrument.com.cn/x.html', 'source_type': 'unknown',
            'body_status': 'fetched', 'body_text': 'XKA-2200 产品页', 'provider': 'public_web'}


def test_a_query_level_warning_is_not_backed_by_the_task_level_fact():
    """不变量：来源清单里有官方来源，就不许只说"未找到官方来源"。"""
    gaps = ['no_official_source'] * 21
    out = reconcile_official_source_gaps(gaps, [_official()])

    assert len(out) == 1
    assert 'no_official_source' not in out[0]
    assert '未找到已核实的官方来源' not in out[0]
    assert '官方来源' in out[0]


def test_the_wording_matches_what_actually_happened():
    with_official = reconcile_official_source_gaps(['no_official_source'], [_official()])
    without = reconcile_official_source_gaps(['no_official_source'], [_junk()])

    assert '部分检索未取得官方来源' in with_official[0]
    assert '本任务已取得 1 个官方来源' in with_official[0]
    assert without[0] == '本任务未取得已核实的官方来源'


def test_the_reconciled_sentence_reads_the_same_after_the_gap_dictionary():
    """收敛结果要能直接给人看：再过一遍 ``_human_gap`` 不许变成另一句话。"""
    out = reconcile_official_source_gaps(['no_official_source'] * 3, [_official()])
    assert _human_gap(out[0]) == out[0]


def test_the_other_gaps_are_not_touched():
    gaps = ['dns_not_found', 'no_official_source', 'empty_body']
    out = reconcile_official_source_gaps(gaps, [_official()])

    assert [g for g in out if g in ('dns_not_found', 'empty_body')] == ['dns_not_found', 'empty_body']


def test_a_run_without_the_warning_is_returned_unchanged():
    gaps = ['dns_not_found', 'empty_body']
    assert reconcile_official_source_gaps(gaps, [_official()]) == gaps


def test_reconciling_twice_does_not_change_the_sentence(research):  # noqa: F401
    """报告读的时候会再收敛一次，所以它必须幂等。"""
    once = reconcile_official_source_gaps(['no_official_source'] * 5, [_official()])
    twice = reconcile_official_source_gaps(once, [_official()])
    third = reconcile_official_source_gaps(twice, [_official()])

    assert once == twice == third


def test_the_marker_is_recognised_in_both_its_forms():
    """历史 run 存的是代号；已经被读过一次的 run 存的是中文说法。"""
    assert is_no_official_source_gap('no_official_source') is True
    assert is_no_official_source_gap('这一次检索未取得官方来源') is True
    assert is_no_official_source_gap('dns_not_found') is False
    assert is_no_official_source_gap(None) is False
    assert is_no_official_source_gap({'a': 1}) is False


def test_the_original_per_query_warnings_survive_on_the_run(research):  # noqa: F811
    """收敛的只是任务级缺口；逐次检索的原始告警一个字都不改。"""
    service, run, _, _ = research
    run['sources'] = [_official()]
    run['gaps'] = ['no_official_source', 'no_official_source']
    run['search_attempts'] = [
        {'query': 'Leica DMi1 官网 公开价格', 'provider': 'public_web', 'status': 'ok',
         'results': 5, 'warnings': ['no_official_source']},
    ]

    run['gaps'] = reconcile_official_source_gaps(run['gaps'], run['sources'])

    assert run['search_attempts'][0]['warnings'] == ['no_official_source']
    assert not any(g == 'no_official_source' for g in run['gaps'])


def test_the_count_uses_the_document_level_test_not_the_type_field():
    """数的是"这份正文是不是正式公告"，不是 ``source_type`` 字段的字面值。

    只用字段会被两件事骗：``unknown`` 里其实有公告，``tender_document`` 里
    也有聚合站单页。``source_publication_tier`` 读的是正文。
    """
    misspelled = {**_official(), 'source_type': 'unknown'}
    assert official_source_count([misspelled]) == 1
    assert official_source_count([_junk()]) == 0
    assert official_source_count([{**_official(), 'body_status': 'failed'}]) == 0
    assert official_source_count([]) == 0
    assert official_source_count(None) == 0


def test_the_report_no_longer_contradicts_its_own_source_list(research):  # noqa: F811
    """报告那一侧：读一份**存下来的** run 时也要收敛（历史缺口改不掉了）。

    这里断言的是 ``report()`` 真正读的那份数据与它派生的未完成事项，
    不去碰 fixture 自己那条 run 上还挂着的别的缺口。
    """
    service, run, _, _ = research
    run['gaps'] = ['no_official_source'] * 20
    service.persist(run)

    stored = service.get(run['task_id'], run['id'])
    stored['sources'] = [_official()]
    reconciled = reconcile_official_source_gaps(stored['gaps'], stored['sources'])
    printed = json.dumps(report_blockers({'gaps': reconciled}), ensure_ascii=False)

    assert '部分检索未取得官方来源；本任务已取得 1 个官方来源，见来源清单' in printed
    assert '未找到已核实的官方来源' not in printed


def test_the_report_path_actually_reconciles_what_it_reads(research):  # noqa: F811
    """``report()`` 里那一行调用是真的在跑（不是只有单测函数对）。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_official()]
    run['prices'] = []
    run['gaps'] = ['no_official_source'] * 20
    service.persist(run)

    service.report(run['task_id'], run['id'])

    stored_gaps = service.get(run['task_id'], run['id']).get('gaps') or []
    assert 'no_official_source' not in stored_gaps
    assert any('官方来源' in gap for gap in stored_gaps)
