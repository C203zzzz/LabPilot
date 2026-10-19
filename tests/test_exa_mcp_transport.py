import json
from types import SimpleNamespace
import urllib.error

import pytest

from backend.infrastructure import exa_mcp
from backend.domain.search import SearchQuery
from backend.infrastructure.search_providers import ExaMCPSearchProvider, ProviderConfigurationError


def test_sse_notifications_do_not_hide_matching_search_result():
    content = {"content": [{"type": "text", "text": "Title: Test\nURL: https://example.org\nHighlights:\nA2"}]}
    data = ': ping\n\ndata: {"method":"notifications/progress"}\n\nevent: message\ndata: ' + json.dumps({"jsonrpc": "2.0", "id": 1, "result": content}) + '\n\n'
    assert exa_mcp.decode_rpc_response(data.encode()) == content


@pytest.mark.parametrize('response', [
    {"id": 2, "result": {"content": []}},
    {"id": 1, "error": {"message": "provider detail"}},
    {"id": 1, "result": {"isError": True, "content": []}},
    {"id": 1, "result": None},
])
def test_protocol_and_tool_failures_are_visible(response):
    with pytest.raises(ValueError):
        exa_mcp.decode_rpc_response(json.dumps(response).encode())


def test_non_utf8_response_is_sanitized():
    with pytest.raises(ValueError, match='not valid UTF-8'):
        exa_mcp.decode_rpc_response(b'\xff\xfe')


def test_http_request_is_bounded_and_does_not_require_local_cli(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, size):
            seen['size'] = size
            return b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'

    def open_request(request, timeout):
        seen.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(exa_mcp.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=open_request))
    assert exa_mcp.search_exa(SimpleNamespace(text='GDR525C 成交', limit=3), 8) == {'content': []}
    assert seen['request'].full_url == exa_mcp.ENDPOINT
    assert json.loads(seen['request'].data)['params']['arguments']['query'] == 'GDR525C 成交'
    assert seen['timeout'] == 8
    assert seen['size'] == exa_mcp.MAX_RESPONSE_BYTES + 1


def test_parser_keeps_highlights_and_canonicalizes_and_deduplicates_urls():
    result = {"content": [{"type": "text", "text": """
Title: Product
URL: HTTPS://Example.ORG/path/?utm_source=exa&b=2&a=1#fragment
Highlights:
first line
second line
---
Title: Duplicate
URL: https://example.org/path?a=1&b=2
Highlights: ignored duplicate
"""}]}
    assert exa_mcp.parse_search_results(result) == [{
        "url": "https://example.org/path?a=1&b=2",
        "title": "Product",
        "snippet": "first line second line",
    }]


def test_parser_preserves_multiple_url_lines_and_strips_wrapping_quotes():
    result = {"content": [{"type": "text", "text": (
        'Title: One\nURL: https://example.org/one"\n'
        'Highlights: first\nURL: https://example.org/two\nHighlights: second'
    )}]}
    assert exa_mcp.parse_search_results(result) == [
        {"url": "https://example.org/one", "title": "One", "snippet": "first"},
        {"url": "https://example.org/two", "title": "", "snippet": "second"},
    ]


@pytest.mark.parametrize('url', [
    'https://user:password@example.org/secret',
    'http://example.org:bad-port/path',
    'https://',
    'javascript:alert(1)',
    'http://127.0.0.1/admin',
    'http://169.254.169.254/latest/meta-data',
    'http://10.1.2.3/private',
    'http://localhost/internal',
    'http://service.local/internal',
    'http://[::1]/admin',
])
def test_parser_drops_malicious_or_malformed_urls(url):
    result = {"content": [{"type": "text", "text": f"Title: Bad\nURL: {url}\nHighlights: no"}]}
    assert exa_mcp.parse_search_results(result) == []


def test_parser_result_limit_is_finite_and_resilient_to_invalid_values():
    records = "\n".join(
        f"Title: R{i}\nURL: https://example.org/{i}\nHighlights: S{i}"
        for i in range(150)
    )
    result = {"content": [{"type": "text", "text": records}]}
    assert len(exa_mcp.parse_search_results(result, limit=3)) == 3
    assert len(exa_mcp.parse_search_results(result, limit=float('inf'))) == 10
    assert len(exa_mcp.parse_search_results(result, limit=1000)) == exa_mcp.MAX_RESULTS


@pytest.mark.parametrize('text', [None, 123])
def test_transport_rejects_non_text_queries_without_network(text, monkeypatch):
    monkeypatch.setattr(exa_mcp.urllib.request, 'build_opener', lambda *_: pytest.fail('network called'))
    with pytest.raises(ValueError, match='query must be text'):
        exa_mcp.search_exa(SimpleNamespace(text=text, limit=3))


def test_transport_rejects_oversized_query_without_network(monkeypatch):
    monkeypatch.setattr(exa_mcp.urllib.request, 'build_opener', lambda *_: pytest.fail('network called'))
    with pytest.raises(ValueError, match='query exceeds'):
        exa_mcp.search_exa(SimpleNamespace(text='x' * (exa_mcp.MAX_QUERY_CHARS + 1), limit=3))


def test_timeout_and_result_count_are_bounded(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _size):
            return b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'

    def open_request(request, timeout):
        seen.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(exa_mcp.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=open_request))
    exa_mcp.search_exa(SimpleNamespace(text='query', limit=10_000), timeout=float('inf'))
    body = json.loads(seen['request'].data)
    assert body['params']['arguments']['numResults'] == exa_mcp.MAX_RESULTS
    assert seen['timeout'] == 12.0


def test_network_exception_is_sanitized(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError('secret upstream detail')

    monkeypatch.setattr(exa_mcp.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=fail))
    with pytest.raises(ValueError, match='^Exa MCP request failed$') as caught:
        exa_mcp.search_exa(SimpleNamespace(text='query', limit=3))
    assert 'secret upstream detail' not in str(caught.value)


def test_provider_wraps_parser_and_transport_failures_without_leaking_detail():
    def fail(_query, _timeout):
        raise RuntimeError('upstream private response')

    provider = ExaMCPSearchProvider(search_fn=fail)
    with pytest.raises(ProviderConfigurationError, match='^Exa MCP unavailable$') as caught:
        provider.search(SearchQuery('query'))
    assert 'upstream private response' not in str(caught.value)
