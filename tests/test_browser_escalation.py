# -*- coding: utf-8 -*-
"""The last hop of 拿到: a challenge page must be allowed to reach a browser.

Two different questions used to share one predicate. ``terminal_failure``
classifies the *source outcome* -- it decides ``unavailable`` vs ``failed`` and
whether another fetch deserves a budget slot -- and a WAF challenge is indeed
terminal there. But "no other transport can answer this" is a different claim:
a challenge page or a bare 403 is exactly what a browser gets past. Sharing the
predicate meant the one transport that could succeed was never tried.

The optional ``curl_cffi`` transport answers the same problem one layer down
(the TLS handshake itself is fingerprinted). It is not a dependency, so the
default path must be byte-for-byte the stdlib one.
"""
import sys
from types import SimpleNamespace

import pytest
from urllib.error import HTTPError

from backend.infrastructure.page_reader import PageReadError, PageResult, PublicPageReader, escalatable_failure
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


@pytest.mark.parametrize('exc', [
    PageReadError('verification_required', 200),   # challenge served with HTTP 200
    PageReadError('verification_required', 403),   # the common WAF shape
    PageReadError('forbidden', 403),
    HTTPError('https://x.example/a', 403, 'Forbidden', {}, None),
    TimeoutError('timed out'),
    PageReadError('empty_shell', 200),             # JS-only shell: rendering is the fix
])
def test_a_transport_that_might_succeed_is_escalated(monkeypatch, exc):
    reader = PublicPageReader(browser=True)
    rendered = []
    monkeypatch.setattr(reader, 'read_http', lambda *_, **__: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(reader, 'read_browser',
                        lambda *_, **__: rendered.append(1) or PageResult(b'<p>body</p>', 'text/html', 'body'))
    result = reader.read('https://buyer.edu.cn/award')
    assert result.metadata['http_failure'] == getattr(exc, 'kind', type(exc).__name__)
    assert rendered == [1]                      # the browser transport was given the URL


@pytest.mark.parametrize('exc', [
    PageReadError('login_required', 200),
    PageReadError('not_found', 404),
    PageReadError('page_too_large', 200),
    HTTPError('https://x.example/a', 404, 'missing', {}, None),
    HTTPError('https://x.example/a', 401, 'unauthorized', {}, None),
    HTTPError('https://x.example/a', 419, 'expired', {}, None),
])
def test_a_failure_no_transport_can_fix_is_not_escalated(monkeypatch, exc):
    reader = PublicPageReader(browser=True)
    rendered = []
    monkeypatch.setattr(reader, 'read_http', lambda *_: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(reader, 'read_browser', lambda *_: rendered.append(1) or PageResult(b'', 'text/html', ''))
    with pytest.raises(type(exc)):
        reader.read('https://buyer.edu.cn/missing')
    assert rendered == []                      # a browser launch is never wasted


def test_escalation_needs_a_configured_browser(monkeypatch):
    reader = PublicPageReader(browser=False)
    monkeypatch.setattr(reader, 'read_http', lambda *_: (_ for _ in ()).throw(PageReadError('verification_required', 403)))
    with pytest.raises(PageReadError):
        reader.read('https://buyer.edu.cn/award')


def test_still_escalates_when_the_page_was_read_from_a_real_challenge(research, monkeypatch):
    """End to end through fetch_source: a challenge page becomes a fetched body."""
    service, run, _, _ = research
    reader = PublicPageReader(browser=True)
    calls = []

    def challenged(*_args, **_kwargs):
        calls.append('http')
        raise PageReadError('verification_required', 403)

    def rendered(*_args, **_kwargs):
        calls.append('browser')
        return PageResult('<html><p>Q10 成交单价 74000 元</p></html>'.encode('utf-8'), 'text/html; charset=utf-8',
                          'Q10 成交单价 74000 元',
                          {'reader': 'browser', 'snapshot_kind': 'rendered_dom', 'status_code': 200})

    monkeypatch.setattr(reader, 'read_http', challenged)
    monkeypatch.setattr(reader, 'read_browser', rendered)
    service.search.reader = reader
    source = service.fetch_source(run, {'url': 'https://buyer.edu.cn/challenged-award', 'source_type': 'unknown'})
    assert calls == ['http', 'browser']
    assert source['body_status'] == 'fetched'
    assert source['read_metadata']['reader'] == 'browser'


def test_the_switch_is_wired_into_the_chain_this_module_builds(monkeypatch, tmp_path):
    from backend.main import create_app
    monkeypatch.setenv('LAB_BROWSER_READING', '1')
    app = create_app(database_path=tmp_path / 'app.sqlite3', storage_dir=tmp_path / 'storage')
    assert app.state.research_service.search.reader.browser is True


def test_a_caller_supplied_service_keeps_its_own_transport(monkeypatch, tmp_path):
    """The reader *is* the body-fetch transport, so injecting one changes meaning.

    Attaching a reader to a service the caller built replaces that service's
    body fetch and its failure vocabulary (``body_status``); the caller owns
    that choice, so the env switch must not reach into it.
    """
    from backend.main import create_app
    from backend.services.official_search import OfficialSearchService
    monkeypatch.setenv('LAB_BROWSER_READING', '1')
    injected = OfficialSearchService(providers=[])
    app = create_app(database_path=tmp_path / 'app.sqlite3', storage_dir=tmp_path / 'storage',
                     search_service=injected)
    assert app.state.research_service.search.reader is None


def test_escalatable_failure_is_not_the_source_outcome_predicate():
    """The two predicates must stay separate: challenges are terminal for one."""
    from backend.infrastructure.page_reader import terminal_failure
    challenge = PageReadError('verification_required', 403)
    assert terminal_failure(challenge) is True          # source outcome: unavailable
    assert escalatable_failure(challenge) is True        # transport: try the browser


def _fake_curl(monkeypatch, pages, guard=None):
    """Install a stub curl_cffi whose responses are queued per URL.

    ``guard`` stands in for the public-host check. The real one resolves DNS and
    rejects private targets (covered by test_public_page_reader), so here it is
    replaced by something that records which hops were validated.
    """
    monkeypatch.setattr('backend.services.research_sources.network_url', guard or (lambda url: url))
    calls = []

    class Response:
        def __init__(self, status_code, content=b'', headers=None, reason='OK'):
            self.status_code, self.content, self.reason = status_code, content, reason
            self.headers = headers or {}

    class Session:
        def __init__(self, impersonate=None):
            self.impersonate = impersonate

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, timeout=None, allow_redirects=None, headers=None):
            calls.append({'url': url, 'impersonate': self.impersonate, 'allow_redirects': allow_redirects,
                          'headers': headers, 'timeout': timeout})
            return pages[url]

    module = SimpleNamespace(requests=SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, 'curl_cffi', module)
    monkeypatch.setitem(sys.modules, 'curl_cffi.requests', module.requests)
    return calls, Response


def test_the_optional_transport_is_off_unless_both_switch_and_package_are_present(monkeypatch):
    from backend.infrastructure.search_providers import impersonation_available
    monkeypatch.delenv('LAB_IMPERSONATE_HTTP', raising=False)
    monkeypatch.setitem(sys.modules, 'curl_cffi', SimpleNamespace())
    assert impersonation_available() is False          # switch off
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    assert impersonation_available() is True           # switch on, package present
    monkeypatch.setitem(sys.modules, 'curl_cffi', None)
    assert impersonation_available() is False          # package missing


def test_impersonated_request_carries_the_guards_across_redirects(monkeypatch):
    from backend.infrastructure.search_providers import PublicWebSearchProvider, impersonation_available
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    pages = {}
    calls, Response = _fake_curl(monkeypatch, pages)
    pages['https://pub.example/a'] = Response(302, headers={'Location': 'https://pub.example/b'})
    pages['https://pub.example/b'] = Response(200, '<p>Q10 成交单价 74000 元</p>'.encode('utf-8'),
                                              headers={'Content-Type': 'text/html; charset=utf-8'})
    provider = PublicWebSearchProvider()
    payload, content_type = provider._request('https://pub.example/a', 5)
    assert payload == '<p>Q10 成交单价 74000 元</p>'.encode('utf-8') and content_type == 'text/html; charset=utf-8'
    assert [c['url'] for c in calls] == ['https://pub.example/a', 'https://pub.example/b']
    assert all(c['impersonate'] == 'chrome' and c['allow_redirects'] is False for c in calls)


def test_impersonated_request_revalidates_every_hop_and_obeys_the_guard(monkeypatch):
    """A redirect target must pass the same public-host check as the first URL."""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    checked = []

    def guard(url):
        checked.append(url)
        if '127.0.0.1' in url:
            raise ValueError('拒绝私有地址')
        return url

    pages = {}
    calls, Response = _fake_curl(monkeypatch, pages, guard)
    pages['https://pub.example/a'] = Response(302, headers={'Location': 'http://127.0.0.1:8080/admin'})
    with pytest.raises(ValueError):
        PublicWebSearchProvider()._request('https://pub.example/a', 5)
    assert checked == ['https://pub.example/a', 'http://127.0.0.1:8080/admin']
    assert [c['url'] for c in calls] == ['https://pub.example/a']       # never requested


def test_impersonated_request_turns_a_status_into_the_usual_http_error(monkeypatch):
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    pages = {}
    calls, Response = _fake_curl(monkeypatch, pages)
    pages['https://pub.example/a'] = Response(403, b'verify you are human', reason='Forbidden')
    with pytest.raises(HTTPError) as caught:
        PublicWebSearchProvider()._request('https://pub.example/a', 5)
    assert caught.value.code == 403


def test_impersonated_request_caps_the_payload(monkeypatch):
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    pages = {}
    calls, Response = _fake_curl(monkeypatch, pages)
    pages['https://pub.example/big'] = Response(200, b'x' * (16 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='16MB'):
        PublicWebSearchProvider()._request('https://pub.example/big', 5)


def _stub_stdlib(monkeypatch, payload=b'<html>real page</html>'):
    """Answer the stdlib branch locally, so no test ever opens a socket.

    ``network_url`` is already stubbed by ``_fake_curl``; this covers the second
    half of the branch -- the opener itself.
    """
    import urllib.request

    seen = {}

    class Response:
        headers = {'Content-Type': 'text/html; charset=utf-8'}

        def read(self, *args):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(self, request, timeout=None, *args, **kwargs):
        seen['url'] = request.full_url
        seen['headers'] = dict(request.headers)
        return Response()

    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    return seen


def test_ccgp_hosts_never_take_the_impersonated_transport(monkeypatch):
    """The measured 403 on ``www.ccgp.gov.cn`` is why CCGP is excluded.

    With the switch on and the package importable, a CCGP request must still
    reach the stdlib branch: the impersonated session is never even opened.
    """
    from backend.infrastructure.search_providers import CCGPSearchProvider
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    calls, _Response = _fake_curl(monkeypatch, {})
    seen = _stub_stdlib(monkeypatch)

    payload, content_type = CCGPSearchProvider()._request(
        'http://search.ccgp.gov.cn/bxsearch?searchtype=2&kw=LDZX-50L', 5)

    assert payload == b'<html>real page</html>' and content_type.startswith('text/html')
    assert calls == []                                  # the impersonated session never opened
    assert 'search.ccgp.gov.cn' in seen['url']
    sent = {name.casefold(): value for name, value in seen['headers'].items()}
    assert sent['user-agent'].startswith('Mozilla/5.0')   # the usual public profile
    assert sent['accept-language'].startswith('zh-CN')


@pytest.mark.parametrize('host_url', [
    'https://ccgp.gov.cn.attacker.example/a',           # a lookalike is not CCGP
    'https://zfcg.czt.zj.gov.cn/portal/all',            # a WAF site that needs the profile
    'https://www.ccgp-anhui.gov.cn/portal/all',
])
def test_every_other_host_keeps_the_impersonated_transport(monkeypatch, host_url):
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    monkeypatch.setenv('LAB_IMPERSONATE_HTTP', '1')
    pages = {}
    calls, Response = _fake_curl(monkeypatch, pages)
    pages[host_url] = Response(200, b'<p>page</p>')

    payload, _content_type = PublicWebSearchProvider()._request(host_url, 5)

    assert payload == b'<p>page</p>'
    assert [c['url'] for c in calls] == [host_url]
    assert calls[0]['impersonate'] == 'chrome'


@pytest.mark.parametrize('url,exempt', [
    ('http://search.ccgp.gov.cn/bxsearch?kw=x', True),
    ('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202511/t20251105_25638769.htm', True),
    ('http://download.ccgp.gov.cn/oss/download?uuid=x', True),
    ('https://CCGP.GOV.CN/bxsearch', True),             # case-insensitive host
    ('https://ccgp.gov.cn.attacker.example/a', False),
    ('https://zfcg.czt.zj.gov.cn/portal/all', False),
])
def test_only_the_ccgp_host_family_is_exempt(url, exempt):
    from backend.infrastructure.search_providers import ccgp_exempt_from_impersonation
    assert ccgp_exempt_from_impersonation(url) is exempt

