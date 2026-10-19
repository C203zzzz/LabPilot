# -*- coding: utf-8 -*-
"""对外的话不许把"某一个通道这一次没有结果"说成"整轮检索没有结果"。

取证（``output/sample-b-stability/report.md``）：sample-b 三轮共 6 次 ``empty_results``
**全部**来自 ``deepseek_web`` 一个通道，同一查询下 exa／bocha／public_web 各返回 10 条；
而报告与页面上那句话是「这一次检索没有返回任何结果」，读者只能读成整轮都没结果。
``docs/209`` §3：对外的粒度不许粗于内部事实；也**不许把一次通道失败说成"没有结果"**。

这一组用例守三件事：
  * 哪个通道空就说哪个通道（代号里带通道名，翻译层说人话）；
  * 跑过的通道**全部**为空才是另一句话（它确实是"整轮没有结果"）；
  * 抛异常的通道说失败，不写"没有结果"；
  * 同一通道这一轮的多次空返回合并成一条，不刷屏。
"""
from backend.domain.search import SearchQuery, SearchResult
from backend.services.official_search import FakeSearchProvider, OfficialSearchService
from backend.services.procurement_research import extend_gaps_once
from backend.services.report_library import humanize_gap_text


def _provider(name, results=None, failures=None):
    provider = FakeSearchProvider(results or {}, failures=failures)
    provider.name = name
    return provider


def _warnings(*providers):
    run = OfficialSearchService(providers=list(providers), fetch_body=False).run(SearchQuery('query'))
    return run.warnings


# ---------------------------------------------------------------------------
# 单通道：说清是哪个通道，别读成整轮
# ---------------------------------------------------------------------------

def test_one_empty_channel_is_named_and_is_not_the_whole_round():
    warnings = _warnings(_provider('deepseek_web', {'query': []}))

    assert 'empty_results:deepseek_web' in warnings, warnings
    # 裸代号（不带通道名）会读成"整轮没有结果"——同一份 warnings 里不许再有它。
    assert 'empty_results' not in warnings, warnings

    shown = humanize_gap_text('empty_results:deepseek_web')
    assert '检索通道 deepseek_web' in shown
    assert '没有返回结果' in shown
    assert '整轮' in shown, shown


def test_two_empty_channels_next_to_a_failed_one_are_named_one_by_one():
    """两个通道空 + 一个通道失败：空的那两个按名字说，失败的那个另说一句。

    （只要有**一个**通道有结果，``raw`` 就不为空，这一次 run 不写空结果告警——
    这是既有的并集口径，对外没有说错任何一句。）
    """
    warnings = _warnings(_provider('exa', {'query': []}),
                         _provider('bocha', {'query': []}),
                         _provider('down', failures={'query': TimeoutError('x')}))

    assert 'empty_results:exa,bocha' in warnings, warnings
    assert 'search_channel_failed:down' in warnings, warnings
    # 有失败通道时不许说"全部通道都没有结果"——跑通与否是两件事。
    assert not [w for w in warnings if w.startswith('empty_results_all_channels')], warnings


def test_a_run_that_did_return_results_says_nothing_about_empty_channels():
    warnings = _warnings(_provider('exa', {'query': []}),
                         _provider('public_web', {'query': [SearchResult('https://shop.example/p', 'P', 's')]}))

    assert not [w for w in warnings if w.startswith('empty_results')], warnings


# ---------------------------------------------------------------------------
# 全部通道为空：这才是"整轮没有结果"，单独一句话
# ---------------------------------------------------------------------------

def test_every_channel_empty_is_its_own_sentence():
    warnings = _warnings(_provider('exa', {'query': []}), _provider('bocha', {'query': []}))

    assert 'empty_results_all_channels:exa,bocha' in warnings, warnings
    assert 'empty_results' not in warnings, warnings

    shown = humanize_gap_text('empty_results_all_channels:exa,bocha')
    assert '2 个检索通道' in shown and 'exa、bocha' in shown
    assert shown != humanize_gap_text('empty_results:exa,bocha')


# ---------------------------------------------------------------------------
# 失败：说失败，不说"没有结果"
# ---------------------------------------------------------------------------

def test_a_channel_that_raised_is_reported_as_a_failure_not_as_no_results():
    warnings = _warnings(_provider('deepseek_web', failures={'query': TimeoutError('down')}))

    # 给排查看的那一条（带类名与原文）照旧保留。
    assert any(w.startswith('provider_failed:deepseek_web') for w in warnings), warnings
    assert 'search_channel_failed:deepseek_web' in warnings, warnings
    assert not [w for w in warnings if w.startswith('empty_results')], warnings

    shown = humanize_gap_text('search_channel_failed:deepseek_web')
    assert '检索通道 deepseek_web' in shown and '失败' in shown
    assert '不是“没有结果”' in shown, shown


def test_a_failed_channel_next_to_an_empty_one_is_not_promoted_to_the_whole_round():
    warnings = _warnings(_provider('down', failures={'query': TimeoutError('x')}),
                         _provider('exa', {'query': []}))

    assert 'search_channel_failed:down' in warnings, warnings
    assert 'empty_results:exa' in warnings, warnings
    assert not [w for w in warnings if w.startswith('empty_results_all_channels')], warnings


def test_the_channel_failure_gap_is_not_the_one_the_lane_filters_out():
    """研究链路会丢掉 ``provider_failed:`` 前缀的告警（``procurement_research``）。

    失败句要是也用那个前缀，就会连对外的那一句一起消失——"跳过"等于把事实抹掉
    （``docs/209`` §3/§5）。所以失败句必须是另一个代号。
    """
    kept = [w for w in ['provider_failed:deepseek_web: HTTP 400', 'search_channel_failed:deepseek_web']
            if not w.startswith('provider_failed:')]
    assert kept == ['search_channel_failed:deepseek_web']


# ---------------------------------------------------------------------------
# 同一通道的多次记录合并，不刷屏
# ---------------------------------------------------------------------------

def test_the_same_channel_gap_is_reported_once_per_run():
    run = {'gaps': []}
    extend_gaps_once(run, ['empty_results:deepseek_web', 'body_failed:https://a.example'])
    extend_gaps_once(run, ['empty_results:deepseek_web', 'body_failed:https://a.example'])

    # 通道级告警合并成一条；逐条来源的告警照旧逐条留（它们分别指不同的网址）。
    assert run['gaps'] == ['empty_results:deepseek_web',
                           'body_failed:https://a.example',
                           'body_failed:https://a.example']
