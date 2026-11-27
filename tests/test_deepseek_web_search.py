import io
import json
import urllib.error

from backend.domain.search import SearchQuery
from backend.infrastructure.deepseek import DeepSeekClient, _http_error_detail
from backend.infrastructure.search_providers import DeepSeekGroundedSearchProvider


def _http_400(body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('https://api.deepseek.com/anthropic/v1/messages', 400,
                                  'Bad Request', {}, io.BytesIO(body))


def test_deepseek_web_search_sends_tool_and_keeps_explicit_citations():
    def transport(_key, _endpoint, payload, _timeout):
        assert payload["tools"] == [{"type": "web_search"}]
        assert payload["tool_choice"] == "auto"
        return {
            "choices": [{
                "message": {
                    "content": [{
                        "type": "output_text",
                        "text": "官方页面",
                        "annotations": [{
                            "type": "url_citation",
                            "url": "https://www.tecan.com/infinite-200-pro",
                            "title": "Infinite 200 PRO",
                            "snippet": "官方产品页",
                        }],
                    }],
                },
            }],
        }

    provider = DeepSeekGroundedSearchProvider.from_env(
        env={"DEEPSEEK_API_KEY": "runtime-only", "DEEPSEEK_ENDPOINT": "https://gateway.example/chat/completions", "DEEPSEEK_ENABLE_WEB_SEARCH": "1"}, transport=transport,
    )
    results = provider.search(SearchQuery('"Tecan Infinite 200 PRO" official specifications', limit=3))
    assert len(results) == 1
    assert results[0].url == "https://www.tecan.com/infinite-200-pro"
    assert results[0].provider == "deepseek_web"


def test_deepseek_web_search_does_not_treat_plain_model_text_as_citation():
    def transport(_key, _endpoint, _payload, _timeout):
        content = {"sources": [{"url": "https://hallucinated.example/product", "title": "模型猜测"}]}
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": "runtime-only", "DEEPSEEK_ENDPOINT": "https://gateway.example/chat/completions", "DEEPSEEK_ENABLE_WEB_SEARCH": "1"}, transport=transport)
    assert client.search_web(query="产品官网", limit=5) == []


def test_deepseek_web_search_accepts_tool_result_arguments():
    def transport(_key, _endpoint, _payload, _timeout):
        return {"choices": [{"message": {"tool_calls": [{
            "type": "web_search",
            "function": {"arguments": json.dumps({"results": [{
                "url": "https://www.example.org/spec",
                "title": "规格页",
                "excerpt": "参数正文",
            }]})},
        }]}}]}

    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": "runtime-only", "DEEPSEEK_ENDPOINT": "https://gateway.example/chat/completions", "DEEPSEEK_ENABLE_WEB_SEARCH": "1"}, transport=transport)
    result = client.search_web(query="规格", limit=1)
    assert result[0]["url"] == "https://www.example.org/spec"


def test_deepseek_grounded_provider_is_disabled_without_runtime_key():
    assert DeepSeekGroundedSearchProvider.from_env(env={}) is None


# --------------------------------------------------------------------------
# The provider's own explanation must survive into the error message
# --------------------------------------------------------------------------

def test_the_provider_explanation_reaches_the_error_message():
    """A bare "HTTP 400" is not a diagnosis; the body names the bad field.

    Measured against the real endpoint (probe22): an invalid credential now
    reports ``HTTP 401：{"error":{"message":"Authentication Fails, ..."}}``.
    Without this the whole message was the five characters "HTTP 401".
    """
    detail = _http_error_detail(_http_400(
        b'{"error":{"message":"deepseek-reasoner does not support tool_choice"}}'))
    assert 'does not support tool_choice' in detail
    assert '\n' not in detail
    assert _http_error_detail(_http_400(b'')) == ''


def test_native_search_sends_tool_choice_as_the_working_shape(monkeypatch):
    """Pinned against a live check, not against assumption.

    probe22 asked the real ``/anthropic/v1/messages`` endpoint twice, once with
    ``tool_choice`` and once without: **both succeeded** and both came back with
    ``web_search_tool_result`` blocks, on deepseek-flash, deepseek-reasoner and
    deepseek-chat alike. So ``tool_choice`` is not the reason native search
    fails, and the request keeps the shape that was measured working.
    """
    seen = []

    def fake_call(self, payload):
        seen.append(dict(payload))
        return {'content': [{'type': 'web_search_tool_result', 'content': [
            {'type': 'web_search_result', 'url': 'https://lab.edu.cn/award', 'title': '成交公告'}]}]}

    monkeypatch.setattr(DeepSeekClient, '_call', fake_call)
    client = DeepSeekClient(api_key='runtime-only',
                            endpoint='https://api.deepseek.com/v1/chat/completions')
    results = client.search_web(query='XKA-2200 成交', limit=3)

    assert [row['url'] for row in results] == ['https://lab.edu.cn/award']
    assert seen[0]['tool_choice'] == {'type': 'auto'}
    assert seen[0]['tools'] == [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 1}]
