from backend.main import create_app
from backend.domain.search import SearchResult
from backend.services.official_search import OfficialSearchService
from backend.services.official_search import FakeSearchProvider
from fastapi.testclient import TestClient


def test_search_plan_and_confirmed_source_flow(tmp_path):
    url = "https://www.tecan.com/example-product"
    provider = FakeSearchProvider(
        results={'"Tecan Infinite 200 PRO" official specifications': [SearchResult(url, "Infinite 200 PRO", "official summary")]},
        bodies={url: "Official body with measurable specifications"},
    )
    search = OfficialSearchService(providers=[provider], fetch_body=True)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage", search_service=search)) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购多功能微孔板检测仪"}).json()
        task_id = created["task"]["id"]
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": created["requirement"]}).status_code == 200
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "Tecan Infinite 200 PRO", "text/plain")})
        candidate = client.post(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]
        assert client.post(f"/api/v1/tasks/{task_id}/candidates/{candidate['id']}/decision", json={"status": "confirmed"}).status_code == 200
        plan = client.post(f"/api/v1/tasks/{task_id}/search/plan").json()
        assert plan["status"] == "pending_confirmation" and plan["queries"]
        assert client.get(f"/api/v1/tasks/{task_id}/search/plan").json()["plan_id"] == plan["plan_id"]
        blocked = client.post(f"/api/v1/tasks/{task_id}/search", json={"plan_id": plan["plan_id"], "fetch_body": True})
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "search_plan_not_confirmed"
        confirmed_plan = client.post(f"/api/v1/tasks/{task_id}/search/plan/{plan['plan_id']}/confirm").json()
        assert confirmed_plan["status"] == "confirmed" and confirmed_plan["confirmed_at"]
        searched = client.post(f"/api/v1/tasks/{task_id}/search", json={"plan_id": plan["plan_id"], "fetch_body": True})
        assert searched.status_code == 200
        source = next(source for item in searched.json()["items"] for source in item["sources"])
        assert source["body_status"] == "fetched"
        assert client.post(f"/api/v1/tasks/{task_id}/sources/decisions", json=[{"url": source["url"], "status": "confirmed"}]).status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}/sources/decisions").json()["items"][0]["status"] == "confirmed"
