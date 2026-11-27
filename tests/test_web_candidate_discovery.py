import json

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.deepseek import DeepSeekClient
from backend.services.official_search import FakeSearchProvider, OfficialSearchService
from backend.services.web_candidate_discovery import WebCandidateDiscoveryService
from backend.main import create_app
from fastapi.testclient import TestClient


def _transport(_key, _endpoint, _payload, _timeout):
    content = {"candidates": [{
        "brand": "Molecular Devices", "series": "SpectraMax", "model": "SpectraMax iD5",
        "configuration": "2套", "sources": [{"url": "https://www.ccgp.gov.cn/result", "excerpt": "货物品牌 美谷分子 SpectraMax iD5"}],
    }]}
    return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}


def test_service_rejects_uncited_or_non_verbatim_web_candidates():
    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": "runtime-only"}, transport=_transport)
    sources = [{"url": "https://www.ccgp.gov.cn/result", "title": "中标公告", "source_type": "tender_document",
                "body_status": "fetched", "body_text": "正文不含模型原句"}]
    assert WebCandidateDiscoveryService(client).discover(scenario="采购酶标仪", sources=sources) == []


def test_api_discovers_pending_candidate_only_from_confirmed_body(monkeypatch, tmp_path):
    url = "https://www.ccgp.gov.cn/result"
    query = "酶标仪 中标公告"
    provider = FakeSearchProvider({query: [SearchResult(url, "中标公告", "摘要")]}, {url: "中标公告 货物品牌 美谷分子 SpectraMax iD5"})
    search = OfficialSearchService(providers=[provider], fetch_body=True)
    original = DeepSeekClient.from_env
    monkeypatch.setattr(DeepSeekClient, "from_env", classmethod(lambda cls, **kwargs: original(env={"DEEPSEEK_API_KEY": "runtime-only"}, transport=_transport)))
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage", search_service=search)) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购酶标仪"}).json()
        task_id = created["task"]["id"]
        requirement = created["requirement"]
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}).status_code == 200
        result = client.post(f"/api/v1/tasks/{task_id}/search", json={"query": query, "purpose": "price", "fetch_body": True}).json()
        source_url = result["items"][0]["sources"][0]["url"]
        assert client.post(f"/api/v1/tasks/{task_id}/sources/decisions", json=[{"url": source_url, "status": "confirmed"}]).status_code == 200
        response = client.post(f"/api/v1/tasks/{task_id}/search/discover-candidates", json={"source_urls": [source_url]})
        assert response.status_code == 200
        candidate = response.json()["items"][0]
        assert candidate["model"] == "SpectraMax iD5"
        assert candidate["status"] == "pending_confirmation"
        assert candidate["sources"][0]["document_id"] == url
