import json
import secrets

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.domain.search import SearchResult, SourceType
from backend.services.official_search import OfficialSearchService, FakeSearchProvider, classify_source


def test_no_document_requirement_search_and_chat(tmp_path, monkeypatch):
    calls = []
    def analyze(self, **kwargs):
        calls.append((self.max_tokens, self.json_retries, kwargs))
        return {"ok": True} if self.max_tokens == 32 else {"reply": "请在采购条件中补充预算。"}
    monkeypatch.setattr(DeepSeekClient, "analyze", analyze)
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files', search_service=OfficialSearchService(FakeSearchProvider()))
    with TestClient(app) as client:
        task = client.post('/api/v1/tasks', json={'scenario': '采购酶标仪'}).json()
        root = '/api/v1/tasks/' + task['task']['id']
        assert client.post(root + '/requirements/confirm', json={'requirement': task['requirement']}).status_code == 200
        plan = client.post(root + '/search/plan').json()
        assert {q['purpose'] for q in plan['queries']} == {'official', 'price', 'tender', 'discovery'}
        assert len(plan['queries']) == 4
        assert client.post('/api/v1/providers/reasoning/health-check', json={'api_key': 'test-memory-only'}).status_code == 200
        answer = client.post(root + '/chat', json={'message': '我该填写什么？'})
        assert answer.status_code == 200 and '预算' in answer.json()['reply']
        assert calls[0][:2] == (32, 0) and calls[1][:2] == (2048, 0)
        assert 'test-memory-only' not in (tmp_path / 'app.db').read_bytes().decode('utf-8', errors='ignore')
        preflight = client.options(root + '/final-decision', headers={'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'PUT'})
        assert preflight.status_code == 200
        assert client.post(root + '/chat', json={'message': '   '}).status_code == 422


def test_model_cache_budget_and_no_automatic_retry(monkeypatch):
    calls = []
    def transport(*args):
        calls.append(args)
        return {'choices': [{'message': {'content': '{"ok": true}'}}]}
    client = DeepSeekClient(api_key=secrets.token_hex(12), transport=transport)
    monkeypatch.setenv('LAB_MODEL_CALLS_PER_HOUR', '1')
    assert client.json_retries == 0
    assert client.analyze(system_prompt='json', user_prompt='same') == {'ok': True}
    assert client.analyze(system_prompt='json', user_prompt='same') == {'ok': True}
    assert len(calls) == 1
    with pytest.raises(DeepSeekError, match='上限'):
        client.analyze(system_prompt='json', user_prompt='different')
    assert len(calls) == 1


def test_official_deepseek_web_requires_server_tool_results():
    calls = []
    def transport(key, endpoint, payload, timeout):
        calls.append((endpoint, payload))
        return {'content': [{'type': 'text', 'text': 'https://invented.example'}]}
    with pytest.raises(DeepSeekError) as error:
        DeepSeekClient(api_key='test', transport=transport).search_web(query='equipment')
    assert error.value.code == 'web_search_not_executed'
    assert calls[0][0] == 'https://api.deepseek.com/anthropic/v1/messages'
    assert calls[0][1]['tools'][0]['type'] == 'web_search_20250305'


def test_real_vision_and_text_share_call_limit(monkeypatch):
    from backend.services.vision import (
        OpenAICompatibleVisionAdapter,
        VisionAdapterError,
        vision_json_retries,
        vision_timeout,
    )
    monkeypatch.setenv('LAB_MODEL_CALLS_PER_HOUR', '1')
    key = secrets.token_hex(16)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: Response())
    text = DeepSeekClient(api_key=key)
    text.analyze(system_prompt='json', user_prompt='test')
    # 生产上真正读图的适配器是运行时从思考模型客户端现造的：
    # 默认一次重试，并且可以被 LAB_VISION_JSON_RETRIES 覆盖。直接构造
    # （``OpenAICompatibleVisionAdapter(key, ...)``）仍是不重试，谁构造谁负责。
    vision = OpenAICompatibleVisionAdapter(
        api_key=key, endpoint=text.endpoint, model=text.model,
        timeout=max(text.timeout, vision_timeout(values={})),
        json_retries=vision_json_retries(values={}),
    )
    assert vision.json_retries == 1
    assert vision_json_retries(values={'LAB_VISION_JSON_RETRIES': '2'}) == 2
    with pytest.raises(VisionAdapterError) as error:
        vision._call({'model': text.model, 'messages': [{'role': 'user', 'content': 'image'}]})
    assert error.value.code == 'provider_budget_exceeded'


def test_untrusted_pdf_and_lookalike_not_official():
    assert classify_source('https://unknown.example/manual.pdf') is SourceType.UNKNOWN
    assert classify_source('https://tecan.com.attacker.example/spec') is SourceType.UNKNOWN
    assert classify_source('https://www.tecan.com/manual.pdf') is SourceType.MANUFACTURER_DOWNLOAD


def test_web_parameter_requires_body_and_can_be_reviewed(tmp_path):
    url = 'https://www.tecan.com/spec'
    body = 'Tecan A-100 检测波长 600 nm'
    provider = FakeSearchProvider({'query': [SearchResult(url, '产品参数', 'summary')]}, {url: body})
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files', search_service=OfficialSearchService(provider))
    with TestClient(app) as client:
        task = client.post('/api/v1/tasks', json={'scenario': '采购检测设备'}).json()
        root = '/api/v1/tasks/' + task['task']['id']
        client.post(root + '/requirements/confirm', json={'requirement': task['requirement']})
        client.post(root + '/documents', files={'file': ('spec.txt', body, 'text/plain')})
        candidate = client.post(root + '/candidates').json()['items'][0]
        client.post(root + '/search', json={'query': 'query'})
        payload = {'candidate_id': candidate['id'], 'field_name': '检测波长', 'value': '600 nm', 'value_type': 'text', 'url': url, 'excerpt': body}
        assert client.post(root + '/sources/fact-drafts', json=payload).status_code == 409
        client.post(root + '/sources/decisions', json=[{'url': url, 'status': 'confirmed'}])
        draft = client.post(root + '/sources/fact-drafts', json=payload)
        assert draft.status_code == 201
        fact = draft.json()
        assert fact['status'] == 'pending_review'
        assert client.post(root + '/facts/' + fact['fact_id'] + '/review', json={'action': 'confirm', 'reviewer_id': 'test'}).status_code == 200
        assert client.post(root + '/sources/fact-drafts', json={**payload, 'value': '900 nm'}).status_code == 422
