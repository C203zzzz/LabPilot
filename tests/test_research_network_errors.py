import socket
import ssl
from urllib.error import URLError

import pytest

from tests.test_reliability_development import research


def test_browser_fallback_enters_procurement_backend_and_cache_is_free(research, monkeypatch):
    from backend.infrastructure.page_reader import PageResult, PublicPageReader
    service, run, _, _ = research
    reader = PublicPageReader(browser=True)
    calls = []

    def fail_http(*_args):
        calls.append('http')
        raise TimeoutError('http timed out')

    def rendered(*_args):
        calls.append('browser')
        html = '<html><body><h1>Q10 成交公告</h1><p>预算 12000 元，仅为预算。</p></body></html>'
        return PageResult(
            html.encode(), 'text/html; charset=utf-8', 'Q10 成交公告 预算 12000 元，仅为预算。',
            {'reader': 'browser', 'snapshot_kind': 'rendered_dom', 'status_code': 200},
        )

    monkeypatch.setattr(reader, 'read_http', fail_http)
    monkeypatch.setattr(reader, 'read_browser', rendered)
    service.search.reader = reader
    before = run['usage']['fetch']
    url = 'https://buyer.edu.cn/rendered-award'
    first = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})
    assert calls == ['http', 'browser']
    assert run['usage']['fetch'] == before + 2
    assert first['body_status'] == 'fetched'
    assert first['read_metadata']['reader'] == 'browser'
    assert first['read_metadata']['http_failure'] == 'TimeoutError'
    assert first['snapshot']['kind'] == 'rendered_dom'

    # A verified body-cache hit reuses the exact rendered DOM snapshot without
    # spending another fetch or silently re-running the browser.
    second = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})
    assert second['cache_hit'] is True
    assert second['body_text'] == first['body_text']
    assert calls == ['http', 'browser']
    assert run['usage']['fetch'] == before + 2


def test_browser_fallback_cannot_exceed_fetch_budget(research, monkeypatch):
    from backend.infrastructure.page_reader import PageResult, PublicPageReader
    from backend.services.procurement_research import ResearchStopped
    service, run, _, _ = research
    reader = PublicPageReader(browser=True)
    browser_calls = []
    monkeypatch.setattr(reader, 'read_http', lambda *_: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(reader, 'read_browser', lambda *_: browser_calls.append(1) or PageResult(b'<p>body</p>', 'text/html', 'body'))
    service.search.reader = reader
    run['limits']['fetch'] = run['usage']['fetch'] + 1
    with pytest.raises(ResearchStopped):
        service.fetch_source(run, {'url': 'https://buyer.edu.cn/budget-boundary'})
    assert browser_calls == []


@pytest.mark.parametrize('kind,status', [('http_403', 403), ('http_404', 404), ('verification_required', 200), ('login_required', 200)])
def test_browser_terminal_failure_does_not_lock_supplement_forever(research, kind, status):
    from types import SimpleNamespace
    from backend.infrastructure.page_reader import PageReadError
    from backend.services.supplemental_research import eligibility
    service, run, _, _ = research
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        raise PageReadError(kind, status)
    service.search.reader = SimpleNamespace(read=read)
    source = service.fetch_source(run, {'url': 'https://restricted.example/page'})
    assert source['body_status'] == 'unavailable' and source['failure_kind'] == kind
    run['sources'].append(source)
    service.recover_sources(run)
    service.report(run['task_id'], run['id'])
    assert len(calls) == 1
    assert eligibility(service.repo, run)['allowed']


@pytest.mark.parametrize('cause,expected', [
    (socket.gaierror(socket.EAI_NONAME, 'not found'), 'dns_not_found'),
    (TimeoutError('timed out'), 'TimeoutError'),
    (ssl.SSLEOFError(8, 'unexpected EOF'), 'tls_SSLEOFError'),
])
def test_wrapped_network_errors_retain_the_actual_failure_type(research, cause, expected):
    service, run, provider, _ = research
    def fail(*args):
        raise URLError(cause)
    provider.fetch = fail
    source = service.fetch_source(run, {'url': 'https://broken.example/a'})
    assert source['failure_kind'] == expected
    assert source['body_status'] == 'failed'


def test_tls_failure_requires_repeat_and_two_other_working_https_hosts(research):
    service, run, provider, _ = research
    def fail(*args):
        raise URLError(ssl.SSLEOFError(8, 'unexpected EOF'))
    provider.fetch = fail
    source = service.fetch_source(run, {'url': 'https://broken.example/a'})
    run['sources'] = [source, {'url': 'https://broken.example/b', 'body_status': 'fetched'},
                      {'url': 'http://plain.example/a', 'body_status': 'fetched'},
                      {'url': 'https://working.example/a', 'body_status': 'fetched'}]
    service.recover_sources(run)
    assert run['unresolved_source_errors']
    assert run['sources'][0]['body_status'] == 'failed'
    run['round'] += 1
    run['sources'].append({'url': 'https://another.example/a', 'body_status': 'fetched'})
    service.recover_sources(run)
    assert not run['unresolved_source_errors']
    assert run['sources'][0]['body_status'] == 'unavailable'
    assert run['sources'][0]['failure_kind'] == 'tls_SSLEOFError'
    assert run['unavailable_sources'][0]['url'] == source['url']
    assert '不等于未检索到产品' in run['gaps'][-1]


def test_recovery_postprocess_failure_is_scoped_to_one_source(research, monkeypatch):
    service, run, _, _ = research
    first_url = 'https://broken.example/first'
    second_url = 'https://broken.example/second'
    run['sources'] = [
        {'url': first_url, 'body_status': 'failed', 'failure_kind': 'TimeoutError'},
        {'url': second_url, 'body_status': 'failed', 'failure_kind': 'TimeoutError'},
    ]
    monkeypatch.setattr(
        service, 'fetch_source',
        lambda _run, source: {**source, 'body_status': 'fetched', 'body_text': 'Q10'})
    persist_calls = []

    def persist(value):
        persist_calls.append(value)
        if len(persist_calls) == 1:
            raise ValueError('来源状态写回失败')

    monkeypatch.setattr(service, 'persist', persist)

    service.recover_sources(run)

    assert [source['body_status'] for source in run['sources']] == ['fetched', 'fetched']
    assert run['failures'][-1]['code'] == 'source_skipped'
    assert first_url in run['gaps'][-1]
