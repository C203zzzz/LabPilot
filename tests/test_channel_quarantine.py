"""A channel that refuses the account or the request shape must leave the chain.

Measured context (``设计规范``, 5 real tasks): ``tavily`` answered HTTP 432
("This request exceeds your plan's set usage limit") and ``deepseek_web``
answered HTTP 400 ("'function' is a required property") on every single call,
and each task spent three discovery calls per channel before the run-local
three-strike rule quarantined them -- 30 of that batch's 200 budgeted search
calls, all of them known in advance to be wasted.

These tests pin the replacement: one refusal takes the channel out for the rest
of the process, and no later caller spends anything finding that out again.
"""
import io
import urllib.error

import pytest

from backend.domain.search import SearchQuery
from backend.infrastructure.deepseek import DeepSeekError
from backend.infrastructure.search_providers import (
    FATAL_CHANNEL_QUARANTINE_MARKER,
    ProviderConfigurationError,
    DeepSeekGroundedSearchProvider,
    channel_quarantine_reason,
    clear_channel_quarantine,
    fatal_channel_failure,
    quarantine_channel,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


def _http_error(code: int, body: bytes, url: str = 'https://api.deepseek.com/chat/completions'):
    return urllib.error.HTTPError(url, code, 'error', {}, io.BytesIO(body))


class _QuotaProvider:
    name = 'quota_probe'

    def __init__(self, transport):
        self.transport = transport

    def supports(self, _query):
        return channel_quarantine_reason(self.name) is None

    def search(self, query):
        reason = channel_quarantine_reason(self.name)
        if reason:
            raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {reason}')
        try:
            return self.transport(query.text, query.limit)
        except urllib.error.HTTPError as exc:
            failure = fatal_channel_failure(exc)
            if failure:
                quarantine_channel(self.name, failure)
                raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {failure}') from exc
            raise


def _probe(transport):
    return _QuotaProvider(transport=transport)


def test_fatal_plan_limit_quarantines_the_channel_after_one_call():
    calls = []

    def transport(_query, _limit):
        calls.append(1)
        raise _http_error(432, b'{"detail":{"error":"This request exceeds your plan\'s set usage limit."}}')

    provider = _probe(transport)
    with pytest.raises(ProviderConfigurationError, match=FATAL_CHANNEL_QUARANTINE_MARKER):
        provider.search(SearchQuery('XKA-2200 中标 单价'))
    assert len(calls) == 1
    assert 'plan' in (channel_quarantine_reason('quota_probe') or '')

    assert provider.supports(SearchQuery('XKA-2200 中标 单价')) is False
    assert _probe(transport).supports(SearchQuery('XKA-2200 中标 单价')) is False
    with pytest.raises(ProviderConfigurationError, match=FATAL_CHANNEL_QUARANTINE_MARKER):
        _probe(transport).search(SearchQuery('XKA-2200 中标 单价'))
    assert len(calls) == 1, 'a quarantined channel must not spend another request'


def test_transient_provider_failure_is_still_worth_its_retries():
    calls = []

    def transport(_query, _limit):
        calls.append(1)
        raise _http_error(503, b'temporarily unavailable')

    provider = _probe(transport)
    for _ in range(3):
        with pytest.raises(urllib.error.HTTPError):
            provider.search(SearchQuery('XKA-2200 中标 单价'))
    assert channel_quarantine_reason('quota_probe') is None
    assert provider.supports(SearchQuery('XKA-2200 中标 单价')) is True
    assert len(calls) == 3


def test_deepseek_web_rejected_tool_payload_quarantines_the_channel():
    calls = []

    class RejectingClient:
        def search_web(self, **_kwargs):
            calls.append(1)
            raise DeepSeekError(
                'provider_error',
                'DeepSeek 服务返回 HTTP 400：{"error":{"code":"invalid_parameter_error",'
                '"message":"\'function\' is a required property, expected an object - \'tools.0\'"}}')

    provider = DeepSeekGroundedSearchProvider(RejectingClient())
    with pytest.raises(ProviderConfigurationError, match=FATAL_CHANNEL_QUARANTINE_MARKER):
        provider.search(SearchQuery('XKA-2200 中标 单价'))
    assert provider.supports(SearchQuery('XKA-2200 中标 单价')) is False
    assert 'tools.0' in (channel_quarantine_reason('deepseek_web') or '')
    assert calls == [1]


def test_deepseek_web_transient_error_does_not_quarantine():
    class FlakyClient:
        def search_web(self, **_kwargs):
            raise DeepSeekError('provider_timeout', '联网检索请求超时')

    provider = DeepSeekGroundedSearchProvider(FlakyClient())
    with pytest.raises(ProviderConfigurationError) as raised:
        provider.search(SearchQuery('XKA-2200 中标 单价'))
    assert FATAL_CHANNEL_QUARANTINE_MARKER not in str(raised.value)
    assert channel_quarantine_reason('deepseek_web') is None
    assert provider.supports(SearchQuery('XKA-2200 中标 单价')) is True


def test_an_explicit_clear_puts_the_channel_back():
    provider = _probe(lambda *_args: (_ for _ in ()).throw(_http_error(401, b'auth')))
    with pytest.raises(ProviderConfigurationError):
        provider.search(SearchQuery('q'))
    assert provider.supports(SearchQuery('q')) is False
    clear_channel_quarantine('quota_probe')
    assert provider.supports(SearchQuery('q')) is True


# --------------------------------------------------------------------------
# The budget account, through the real research service
# --------------------------------------------------------------------------

def test_a_dead_channel_costs_one_discovery_call_not_three_per_round(research):
    """``设计规范``: 5 tasks x 2 dead channels x 3 strikes = 30 spent calls.

    After the fix the same channel is asked exactly once, and every later query
    -- in this run and in the next one -- finds it already gone.
    """
    service, run, _, _ = research
    calls = []

    def transport(_query, _limit):
        calls.append(_query)
        raise _http_error(432, b'{"detail":{"error":"This request exceeds your plan\'s set usage limit."}}')

    class Empty:
        name = 'working'

        def search(self, query):
            return []

    service.search.providers = [Empty(), _probe(transport)]
    service.fetch_source = lambda run, source: {
        **source, 'body_status': 'fetched', 'body_text': 'Q10 成交公告', 'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    before = run['usage']['search']
    for attempt in range(3):
        service.query(run, f'dead channel query {attempt}', 'tender', target={'model': 'Q10'})

    assert len(calls) == 1, 'the dead channel must be asked once, not three times'
    assert run['usage']['search'] == before + 4
    assert 'quota_probe' in run['unavailable_search_providers']
    statuses = [a.get('status') for a in run['search_attempts'] if a.get('provider') == 'quota_probe']
    assert statuses == ['failed', 'quarantined_skipped', 'quarantined_skipped']

    run.pop('unavailable_search_providers', None)
    run['provider_failure_counts'] = {}
    before_next = run['usage']['search']
    service.query(run, 'a later run asks the same thing', 'tender', target={'model': 'Q10'})
    assert len(calls) == 1, 'a new round must not re-probe a channel the process already buried'
    assert run['usage']['search'] == before_next + 1, 'only the living channel may cost a call'
    skipped = [a for a in run['search_attempts'] if a.get('provider') == 'quota_probe'][-1]
    assert skipped['status'] == 'channel_quarantined'
    assert 'plan' in skipped['quarantine_reason']
    assert 'quota_probe' in run['unavailable_search_providers']
