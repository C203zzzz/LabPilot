import pytest

from backend.domain.search import SearchQuery
from backend.infrastructure import exa_mcp
from backend.infrastructure.search_providers import ExaMCPSearchProvider, ProviderConfigurationError


def test_parser_keeps_title_and_highlights_after_url_and_deduplicates():
    result = {
        "content": [{"type": "text", "text": (
            "Title: Official product\n"
            "URL: https://example.org/product\n"
            "Published: N/A\n"
            "Highlights:\n"
            "| model X100 | official specification |\n"
            "Title: Duplicate\n"
            "URL: https://example.org/product\n"
        )}]
    }
    rows = exa_mcp.parse_search_results(result, limit=5)
    assert rows == [{
        "url": "https://example.org/product",
        "title": "Official product",
        "snippet": "| model X100 | official specification |",
    }]


def test_parser_rejects_non_http_urls_and_enforces_limit():
    result = {"content": [{"type": "text", "text": (
        "Title: one\nURL: javascript:alert(1)\n"
        "Title: two\nURL: https://example.org/two\n"
        "Title: three\nURL: https://example.org/three\n"
    )}]}
    rows = exa_mcp.parse_search_results(result, limit=1)
    assert [row["url"] for row in rows] == ["https://example.org/two"]


def test_provider_uses_keyless_transport_and_coerces_only_explicit_urls():
    calls = []

    def fake(query, timeout):
        calls.append((query.text, timeout))
        return [{"url": "https://example.org/a", "title": "A", "snippet": "lead"}, {"url": "ftp://bad", "title": "bad"}]

    provider = ExaMCPSearchProvider(search_fn=fake, timeout=4)
    rows = provider.search(SearchQuery("X100", limit=2))
    assert [(row.url, row.provider) for row in rows] == [("https://example.org/a", "exa_mcp")]
    assert calls == [("X100", 4)]


def test_provider_wraps_network_failure_without_leaking_details():
    def fail(_query, _timeout):
        raise RuntimeError("secret response body")

    with pytest.raises(ProviderConfigurationError, match="Exa MCP unavailable") as error:
        ExaMCPSearchProvider(search_fn=fail).search(SearchQuery("X100"))
    assert "secret" not in str(error.value)


def test_provider_disabled_by_runtime_flag():
    assert ExaMCPSearchProvider.from_env(env={"EXA_MCP_ENABLED": "0"}) is None
