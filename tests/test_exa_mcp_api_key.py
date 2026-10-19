"""Optional ``EXA_API_KEY`` support in the Exa MCP transport.

Every test is offline: the opener is replaced with a fake, and no key is real.
The frozen values in ``PRE_CHANGE_REQUEST`` were captured from the code as it
was before this change (``git show 0d094b2:backend/infrastructure/exa_mcp.py``),
so the keyless request can be compared byte for byte.
"""
import traceback
from types import SimpleNamespace

import pytest

from backend.infrastructure import exa_mcp
from backend.domain.search import SearchQuery
from backend.infrastructure.search_providers import ExaMCPSearchProvider, ProviderConfigurationError

QUERY_TEXT = "GDR525C 成交"
KEY = "exa-test-key-not-real-0123456789abcdef"

# Captured from the transport before optional-key support existed.
PRE_CHANGE_REQUEST = {
    "full_url": "https://mcp.exa.ai/mcp",
    "method": "POST",
    "headers": {
        "Content-type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-agent": "Mozilla/5.0 (compatible; LabPilot/1.0)",
    },
    "data_utf8": (
        '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": '
        '{"name": "web_search_exa", "arguments": {"query": "GDR525C \\u6210\\u4ea4", '
        '"numResults": 3, "type": "auto"}}}'
    ),
    "timeout": 8.0,
}


def _render(exc: BaseException) -> str:
    """Render an exception with its cause chain, the way a logger would."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _install_fake_opener(monkeypatch, open_request):
    """Replace the opener and expose the handlers ``search_exa`` installed."""
    captured = {}

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(exa_mcp.urllib.request, "build_opener", build_opener)
    return captured


def _successful_opener(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size):
            seen["size"] = size
            return b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'

    def open_request(request, timeout):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    _install_fake_opener(monkeypatch, open_request)
    return seen


@pytest.mark.parametrize("state", ["absent", "empty", "whitespace"])
def test_request_without_a_key_matches_the_pre_change_bytes(monkeypatch, state):
    """Absent / empty / whitespace-only key: request and behaviour are unchanged."""
    if state == "absent":
        monkeypatch.delenv(exa_mcp.API_KEY_ENV, raising=False)
    elif state == "empty":
        monkeypatch.setenv(exa_mcp.API_KEY_ENV, "")
    else:
        monkeypatch.setenv(exa_mcp.API_KEY_ENV, " \t\r\n ")

    seen = _successful_opener(monkeypatch)
    assert exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3), 8) == {"content": []}

    request = seen["request"]
    assert seen["calls"] == 1  # a missing key never skips the request
    assert request.full_url == PRE_CHANGE_REQUEST["full_url"]
    assert request.get_method() == PRE_CHANGE_REQUEST["method"]
    assert dict(request.headers) == PRE_CHANGE_REQUEST["headers"]
    assert request.data == PRE_CHANGE_REQUEST["data_utf8"].encode("utf-8")
    assert request.unredirected_hdrs == {}
    assert seen["timeout"] == PRE_CHANGE_REQUEST["timeout"]
    assert seen["size"] == exa_mcp.MAX_RESPONSE_BYTES + 1
    assert exa_mcp.ENDPOINT == PRE_CHANGE_REQUEST["full_url"]


def test_configured_key_is_sent_as_a_bearer_header_and_changes_nothing_else(monkeypatch):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)
    seen = _successful_opener(monkeypatch)
    assert exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3), 8) == {"content": []}

    request = seen["request"]
    assert dict(request.headers) == dict(PRE_CHANGE_REQUEST["headers"], Authorization=f"Bearer {KEY}")
    # The key travels in a header only: the endpoint constant is untouched.
    assert request.full_url == PRE_CHANGE_REQUEST["full_url"]
    assert "?" not in request.full_url and KEY not in request.full_url
    assert request.unredirected_hdrs == {}
    assert request.data == PRE_CHANGE_REQUEST["data_utf8"].encode("utf-8")
    assert request.get_method() == PRE_CHANGE_REQUEST["method"]
    assert seen["timeout"] == PRE_CHANGE_REQUEST["timeout"]
    assert seen["size"] == exa_mcp.MAX_RESPONSE_BYTES + 1


def test_surrounding_whitespace_in_the_key_is_stripped(monkeypatch):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, f"\t {KEY} \r\n")
    seen = _successful_opener(monkeypatch)
    exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3), 8)
    request = seen["request"]
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.full_url == PRE_CHANGE_REQUEST["full_url"]


def test_key_is_read_per_request_and_never_cached(monkeypatch):
    seen = _successful_opener(monkeypatch)
    query = SimpleNamespace(text=QUERY_TEXT, limit=3)

    monkeypatch.delenv(exa_mcp.API_KEY_ENV, raising=False)
    exa_mcp.search_exa(query, 8)
    assert "Authorization" not in seen["request"].headers

    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)
    exa_mcp.search_exa(query, 8)
    assert seen["request"].headers["Authorization"] == f"Bearer {KEY}"

    monkeypatch.delenv(exa_mcp.API_KEY_ENV, raising=False)
    exa_mcp.search_exa(query, 8)
    assert "Authorization" not in seen["request"].headers


def test_api_key_reader_maps_absent_and_blank_to_no_key():
    assert exa_mcp.API_KEY_ENV == "EXA_API_KEY"
    assert exa_mcp._api_key({}) == ""
    assert exa_mcp._api_key({exa_mcp.API_KEY_ENV: ""}) == ""
    assert exa_mcp._api_key({exa_mcp.API_KEY_ENV: "   \t\n"}) == ""
    assert exa_mcp._api_key({exa_mcp.API_KEY_ENV: f" {KEY}\n"}) == KEY


@pytest.mark.parametrize("bad_key", [
    f"exa key with a space {KEY}",
    f"exa-中文-key-{KEY}",
    f"exa-\nkey-{KEY}",
    "x" * (exa_mcp.MAX_API_KEY_CHARS + 1),
])
def test_malformed_key_is_refused_without_echoing_it(monkeypatch, bad_key):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, bad_key)
    monkeypatch.setattr(exa_mcp.urllib.request, "build_opener", lambda *_: pytest.fail("network called"))
    with pytest.raises(ValueError, match="^Exa MCP API key is malformed$") as caught:
        exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3))
    assert bad_key not in _render(caught.value)


def test_redirect_refusal_message_never_contains_the_key(monkeypatch):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)
    seen = {}

    def open_request(request, timeout):
        seen["request"] = request
        # Same call the real opener makes: HTTPRedirectHandler.http_error_302
        # invokes the refusing handler installed by ``search_exa``.
        return captured["handlers"][0].redirect_request(
            request, None, 302, "Found", {}, f"https://elsewhere.example/?exaApiKey={KEY}")

    captured = _install_fake_opener(monkeypatch, open_request)
    with pytest.raises(ValueError) as caught:
        exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3))
    assert str(caught.value) == "Exa MCP endpoint redirect refused"
    assert KEY not in _render(caught.value)
    # The request really did carry the key, so the check above is meaningful.
    assert seen["request"].headers["Authorization"] == f"Bearer {KEY}"


def test_http_error_branch_does_not_leak_key(monkeypatch):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)

    def open_request(request, timeout):
        # A verbose server can stuff anything into the URL/message an HTTPError
        # carries; none of it may reach the message this transport raises.
        raise exa_mcp.urllib.error.HTTPError(
            f"{exa_mcp.ENDPOINT}?exaApiKey={KEY}", 429, f"Too Many Requests ({KEY})", {}, None)

    _install_fake_opener(monkeypatch, open_request)
    with pytest.raises(ValueError) as caught:
        exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3))
    assert str(caught.value) == "Exa MCP HTTP 429"
    assert caught.value.__cause__ is None
    assert KEY not in _render(caught.value)


def test_network_error_branch_does_not_leak_key(monkeypatch):
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)

    def open_request(*_args, **_kwargs):
        raise exa_mcp.urllib.error.URLError("upstream refused the connection")

    _install_fake_opener(monkeypatch, open_request)
    with pytest.raises(ValueError) as caught:
        exa_mcp.search_exa(SimpleNamespace(text=QUERY_TEXT, limit=3))
    assert str(caught.value) == "Exa MCP request failed"
    assert KEY not in _render(caught.value)


def test_end_to_end_error_text_with_a_key_has_no_key(monkeypatch):
    """Real transport + real provider, with a key set: nothing renderable leaks it."""
    monkeypatch.setenv(exa_mcp.API_KEY_ENV, KEY)

    def open_request(request, timeout):
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        raise exa_mcp.urllib.error.URLError("connection reset")

    _install_fake_opener(monkeypatch, open_request)
    with pytest.raises(ProviderConfigurationError) as caught:
        ExaMCPSearchProvider(timeout=4).search(SearchQuery(QUERY_TEXT))
    assert str(caught.value) == "Exa MCP unavailable"
    assert KEY not in _render(caught.value)
