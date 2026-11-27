"""Regressions for "the channel did nothing, but everything said ok".

Two halves of one root cause:

* nothing in the run noticed a channel that keeps answering "ok" with nothing,
  so the user saw a healthy search where no evidence was ever collected;
* the keyword the run reports has to be the keyword that was really sent.

The CCGP half used to be worse than empty: the adapter searched the notice
*title* with a catalogue model, which can only ever return zero rows while
still answering HTTP 200. It now searches the announcement body, so a model is
a real keyword -- these tests pin both the newer behaviour and the alerting.
"""
import time
import urllib.parse

import pytest

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.search_providers import (
    CCGPSearchProvider, ProviderConfigurationError,
)
from backend.services.procurement_research import (
    EMPTY_RESULT_ALERT_STREAK, engine_query_for,
)
from tests.test_reliability_development import research


def _keyword(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["kw"][0]


# --------------------------------------------------------------------------
# CCGP: the model is a real keyword now, and telemetry has to say so
# --------------------------------------------------------------------------

def test_a_query_with_nothing_searchable_is_never_sent():
    """It must not be merely empty -- it must never leave the adapter."""
    requested = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (requested.append(url) or (b'', 'text/html')))
    results = provider.search_once(SearchQuery('site:gov.cn filetype:pdf 中标 成交', purpose='tender'))
    assert results == []
    assert requested == []


def test_a_model_only_query_is_now_sent():
    """Body search reached the model, so it is no longer dropped unsent."""
    requested = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (requested.append(url) or (b'', 'text/html')))
    provider.search_once(SearchQuery('"XKA-2200" 成交', purpose='tender',
                                     brand='湘仪', model='XKA-2200'))
    assert [_keyword(u) for u in requested] == ['XKA-2200', 'XKA', '湘仪']


@pytest.mark.parametrize('text,brand,model,expected', [
    ('湘仪 XKA-2200 中标 单价', '湘仪', 'XKA-2200', 'XKA-2200'),
    ('湘仪 XKA-2200 台式离心机 中标 成交 单价', '湘仪', 'XKA-2200', 'XKA-2200'),
    ('知楚A2 生物安全柜 采购结果 site:gov.cn', '知楚', 'A2', 'A2'),
    ('"Q10" 生化培养箱 科研院所 采购 合同 单价', '甲厂', 'Q10', 'Q10'),
])
def test_engine_query_prefers_the_identifier_the_body_index_can_match(text, brand, model, expected):
    assert CCGPSearchProvider.engine_query(text, brand=brand, model=model) == expected


def test_supports_accepts_an_identifier_and_refuses_an_unsearchable_query():
    provider = CCGPSearchProvider()
    assert provider.supports(SearchQuery('湘仪 XKA-2200 台式离心机 中标', purpose='tender',
                                         brand='湘仪', model='XKA-2200'))
    assert provider.supports(SearchQuery('湘仪 XKA-2200 中标', purpose='tender',
                                         brand='湘仪', model='XKA-2200'))
    assert not provider.supports(SearchQuery('site:gov.cn 中标 成交', purpose='tender'))


def test_engine_query_for_reports_the_keyword_the_adapter_really_sent():
    """Telemetry must name what went out, not what was preferred."""
    from backend.services.research_planning import procurement_queries
    queries = procurement_queries({'brand': '甲厂', 'model': 'Q10', 'category': '台式离心机'})
    text = queries[1][0]
    provider = CCGPSearchProvider(transport=lambda url, timeout: (b'', 'text/html'))
    query = SearchQuery(text, purpose='tender', brand='甲厂', model='Q10')
    provider.search_once(query)
    assert 'Q10' in text
    # Nothing matched, so the reported keyword is the first one tried.
    assert engine_query_for(provider, query) == 'Q10'


def test_the_run_records_the_keyword_that_really_went_out(research):
    """End to end: the recorded attempt matches the keyword that was sent."""
    service, run, _, _ = research
    _reset_search_state(run)
    requested = []

    class Recording(CCGPSearchProvider):
        def _request(self, url, timeout):
            requested.append(url)
            return '共找到 0 条内容'.encode('utf-8'), 'text/html'

    service.search.providers = [Recording()]
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 8, 'fetch': 8, 'model': 8}
    run['baseline']['brand'] = '湘仪'
    run['baseline']['model'] = 'XKA-2200'
    service.query(run, '湘仪 XKA-2200 台式离心机 中标 成交 单价 site:gov.cn', 'tender',
                  target={'model': 'XKA-2200', 'category': '台式离心机'})

    attempt = run['search_attempts'][-1]
    # The walk stops as soon as a keyword returns rows. This channel returns
    # none, so every candidate is tried and the reported one is the first --
    # which is the model, because body search made it a real keyword.
    # 「湘仪 台式离心机」是 2026-10-06 新增的组合档，排在单品牌之前
    # （理由见 test_ccgp_provider.py::test_query_ladder_asks_brand_plus_category_before_brand_alone）。
    assert [_keyword(u) for u in requested] == [
        'XKA-2200', 'XKA', '湘仪 台式离心机', '湘仪', '台式离心机']
    assert attempt['engine_query'] == 'XKA-2200'
    assert attempt['status'] == 'ok' and attempt['results'] == 0


# --------------------------------------------------------------------------
# CCGP: the site's rate-limit page stops the channel instead of emptying it
# --------------------------------------------------------------------------

def test_rate_limited_page_pauses_the_channel_instead_of_returning_nothing():
    provider = CCGPSearchProvider(
        transport=lambda url, timeout: ('<html><title>频繁访问!中国政府采购网</title></html>'.encode('utf-8'),
                                        'text/html'))
    with pytest.raises(ProviderConfigurationError):
        provider.search_once(SearchQuery('生物安全柜 中标', purpose='tender'))
    assert provider.paused_until > time.monotonic()


def test_a_paused_channel_fails_loudly_and_touches_no_network(monkeypatch):
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    def forbidden(*args, **kwargs):
        raise AssertionError('a paused channel must not send another request')

    monkeypatch.setattr(PublicWebSearchProvider, '_request', forbidden)
    provider = CCGPSearchProvider(paused_until=time.monotonic() + 60)
    with pytest.raises(ProviderConfigurationError, match='paused'):
        provider.search_once(SearchQuery('生物安全柜 中标', purpose='tender'))


def test_a_pause_is_scoped_to_ccgp_and_does_not_stop_other_hosts(monkeypatch):
    """This adapter is also a generic body reader; its self-restraint must not
    turn into a failure for sources that have nothing to do with the pause."""
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    monkeypatch.setattr(PublicWebSearchProvider, '_request',
                        lambda *args, **kwargs: (b'<html>ok</html>', 'text/html'))
    provider = CCGPSearchProvider(paused_until=time.monotonic() + 60, min_interval=0.0)
    assert provider.fetch_raw('https://lab.edu.cn/award', 5) == (b'<html>ok</html>', 'text/html')
    with pytest.raises(ProviderConfigurationError, match='paused'):
        provider.fetch_raw('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/t1.htm', 5)


# --------------------------------------------------------------------------
# The run: consecutive ok-but-empty calls become a visible gap
# --------------------------------------------------------------------------

def _reset_search_state(run):
    run['gaps'] = []
    run['search_attempts'] = []
    run['provider_empty_streaks'] = {}
    run['provider_result_totals'] = {}
    run.pop('unavailable_search_providers', None)
    run.pop('provider_failure_counts', None)


def _drive(service, run, provider, texts):
    service.search.providers = [provider]
    service.fetch_source = lambda run, source: {**source, 'body_status': 'fetched',
                                                'body_text': 'Q10 离心机采购成交公告',
                                                'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 24, 'fetch': 24, 'model': 8}
    for text in texts:
        service.query(run, text, 'tender', target={'model': 'Q10', 'category': '离心机'})


def _empty_gaps(run, provider_name):
    return [gap for gap in run['gaps'] if provider_name in gap and '0 结果' in gap]


def test_consecutive_ok_but_empty_calls_are_reported(research):
    service, run, _, _ = research
    _reset_search_state(run)

    class Silent:
        name = 'ccgp'

        def search(self, query):
            return []

    _drive(service, run, Silent(), [f'Q10 离心机 成交 {i}' for i in range(EMPTY_RESULT_ALERT_STREAK)])

    assert [a['status'] for a in run['search_attempts']] == ['ok'] * EMPTY_RESULT_ALERT_STREAK
    assert [a['results'] for a in run['search_attempts']] == [0] * EMPTY_RESULT_ALERT_STREAK
    assert run['provider_empty_streaks']['ccgp'] == EMPTY_RESULT_ALERT_STREAK

    gaps = _empty_gaps(run, 'ccgp')
    assert len(gaps) == 1, 'a dead channel is reported once per streak, not on every query'
    assert '从未有产出' in gaps[0]
    assert run['search_attempts'][-1]['warnings'][-1] == 'provider_returned_no_results'


def test_a_channel_that_keeps_producing_is_never_reported(research):
    service, run, _, _ = research
    _reset_search_state(run)

    class Working:
        name = 'ccgp'

        def search(self, query):
            return [SearchResult('https://lab.edu.cn/award', 'Q10 离心机采购成交公告', '实验室设备')]

    _drive(service, run, Working(), [f'Q10 离心机 成交 {i}' for i in range(EMPTY_RESULT_ALERT_STREAK + 1)])

    assert len(run['search_attempts']) == EMPTY_RESULT_ALERT_STREAK + 1  # every call really happened
    assert run['provider_empty_streaks']['ccgp'] == 0
    assert run['provider_result_totals']['ccgp'] == EMPTY_RESULT_ALERT_STREAK + 1
    assert _empty_gaps(run, 'ccgp') == []


def test_below_the_threshold_nobody_is_accused(research):
    """Two empty calls in a run are ordinary; only a streak is evidence."""
    service, run, _, _ = research
    _reset_search_state(run)

    class Silent:
        name = 'ccgp'

        def search(self, query):
            return []

    _drive(service, run, Silent(), [f'Q10 离心机 成交 {i}' for i in range(EMPTY_RESULT_ALERT_STREAK - 1)])

    assert run['provider_empty_streaks']['ccgp'] == EMPTY_RESULT_ALERT_STREAK - 1
    assert _empty_gaps(run, 'ccgp') == []


def test_a_provider_error_is_a_failure_and_never_an_empty_result(research):
    service, run, _, _ = research
    _reset_search_state(run)

    class Blocked:
        name = 'blocked'

        def search(self, query):
            # An ordinary outage, deliberately not an access challenge: a
            # challenge quarantines a provider on its first failure (see
            # test_procurement_multisearch), while this test is about every
            # failing call being recorded as a failure rather than as silence.
            raise ProviderConfigurationError('搜索服务不可用')

    service.search.providers = [Blocked()]
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 24, 'fetch': 24, 'model': 8}
    for i in range(EMPTY_RESULT_ALERT_STREAK):
        with pytest.raises(ValueError, match='搜索接口失败'):
            service.query(run, f'Q10 离心机 成交 {i}', 'tender', target={'model': 'Q10'})

    assert [a['status'] for a in run['search_attempts']] == ['failed'] * EMPTY_RESULT_ALERT_STREAK
    assert run.get('provider_empty_streaks', {}).get('blocked', 0) == 0
    assert _empty_gaps(run, 'blocked') == []


def test_a_channel_left_out_because_it_does_not_apply_is_not_counted(research):
    """``supports`` is false, so the provider is never asked and never blamed."""
    service, run, _, _ = research
    _reset_search_state(run)

    class NotApplicable:
        name = 'ccgp'

        def supports(self, query):
            return False

        def search(self, query):  # pragma: no cover - must never be called
            raise AssertionError('a channel that does not apply must not be called')

    service.search.providers = [NotApplicable()]
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 24, 'fetch': 24, 'model': 8}
    with pytest.raises(ValueError, match='搜索接口失败'):
        service.query(run, 'Q10 离心机 成交', 'tender', target={'model': 'Q10'})

    assert run['search_attempts'] == []
    assert run['provider_empty_streaks'] == {}


def test_a_channel_that_recovers_and_goes_quiet_again_is_reported_twice(research):
    service, run, _, _ = research
    _reset_search_state(run)

    class Flaky:
        name = 'ccgp'
        producing = False

        def search(self, query):
            return [SearchResult('https://lab.edu.cn/award', 'Q10 离心机采购成交公告')] if self.producing else []

    provider = Flaky()
    _drive(service, run, provider, [f'Q10 离心机 成交 {i}' for i in range(EMPTY_RESULT_ALERT_STREAK)])
    assert len(_empty_gaps(run, 'ccgp')) == 1

    provider.producing = True
    _drive(service, run, provider, ['Q10 离心机 恢复产出'])
    assert run['provider_empty_streaks']['ccgp'] == 0

    provider.producing = False
    _drive(service, run, provider, [f'Q10 离心机 再次空转 {i}' for i in range(EMPTY_RESULT_ALERT_STREAK)])
    gaps = _empty_gaps(run, 'ccgp')
    assert len(gaps) == 2
    # The second one is the informative wording: this channel used to produce.
    assert '此前有产出' in gaps[-1]
