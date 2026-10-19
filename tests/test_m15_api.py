from fastapi.testclient import TestClient

from backend.main import create_app


def test_m15_task_document_candidate_flow(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购一台培养设备，预算 2 万元"})
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.txt", "赛默飞 INFINITE 200 PRO", "text/plain")},
        )
        assert uploaded.status_code == 201
        body = uploaded.json()
        assert body["sha256"] and body["original_path"] and "warnings" in body and "extracted_text" in body
        candidates = client.post(f"/api/v1/tasks/{task_id}/candidates")
        assert candidates.status_code == 200
        assert candidates.json()["items"]
        fetched = client.get(f"/api/v1/tasks/{task_id}")
        assert fetched.status_code == 200 and fetched.json()["documents"]


def test_m15_create_app_normalizes_string_storage_path(tmp_path):
    database_path = str(tmp_path / "string-path.sqlite3")
    storage_dir = str(tmp_path / "string-storage")
    with TestClient(create_app(database_path=database_path, storage_dir=storage_dir)) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "上传路径回归"}).json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.txt", "型号 X100", "text/plain")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["original_path"].endswith(".txt")


def test_candidate_decision_is_persisted_and_search_scope_is_confirmed_only(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "Tecan INFINITE 200 PRO", "text/plain")})
        items = client.post(f"/api/v1/tasks/{task_id}/candidates").json()["items"]
        cid = items[0]["id"]
        assert items[0]["status"] == "pending_confirmation"
        response = client.post(f"/api/v1/tasks/{task_id}/candidates/{cid}/decision", json={"status": "confirmed", "note": "用户核对"})
        assert response.status_code == 200 and response.json()["status"] == "confirmed"
        assert client.get(f"/api/v1/tasks/{task_id}/candidates/confirmed").json()["items"][0]["id"] == cid
        fetched = client.get(f"/api/v1/tasks/{task_id}").json()["candidates"]
        assert fetched[0]["status"] == "confirmed" and fetched[0]["note"] == "用户核对"


def test_candidate_batch_decision_supports_reject_and_confirm(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "Tecan A-100 and Tecan B-200", "text/plain")})
        items = client.post(f"/api/v1/tasks/{task_id}/candidates").json()["items"]
        payload = [{"candidate_id": items[0]["id"], "status": "excluded"}, {"candidate_id": items[1]["id"], "status": "confirmed"}]
        result = client.post(f"/api/v1/tasks/{task_id}/candidates/decisions", json=payload)
        assert result.status_code == 200
        assert {item["status"] for item in result.json()["items"]} == {"excluded", "confirmed"}


def test_m15_shutdown_releases_sqlite_file(tmp_path):
    db_path = tmp_path / "lifecycle.sqlite3"
    with TestClient(create_app(database_path=db_path, storage_dir=tmp_path / "storage")) as client:
        assert client.get("/health").status_code == 200
    db_path.unlink()
    reopened = create_app(database_path=db_path, storage_dir=tmp_path / "storage")
    reopened.state.repository.close()
    db_path.unlink()


def test_m15_stable_json_errors_and_health():
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.post("/api/v1/tasks/missing/requirements/confirm", json={"requirement": {}})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "task_not_found"


def test_m15_cors_allows_only_configured_browser_origin(tmp_path):
    app = create_app(
        database_path=tmp_path / "cors.sqlite3",
        storage_dir=tmp_path / "storage",
        cors_origins=("http://127.0.0.1:5173",),
    )
    with TestClient(app) as client:
        allowed = client.options(
            "/api/v1/tasks",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        assert "POST" in allowed.headers["access-control-allow-methods"]

        denied = client.get("/health", headers={"Origin": "https://untrusted.example"})
        assert "access-control-allow-origin" not in denied.headers


def test_m15_history_versions_are_saved_and_queryable(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "history.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task = client.post("/api/v1/tasks", json={"scenario": "采购培养设备"}).json()["task"]["id"]
        base = {"requirement": {"scenario": "采购培养设备", "unknowns": []}, "candidates": [], "evaluations": [], "facts": [], "format": "json"}
        first = client.post(f"/api/v1/tasks/{task}/reports", json=base)
        assert first.status_code == 200
        v1 = first.json()["report"] if first.headers.get("content-type", "").startswith("application/json") else None
        history = client.get(f"/api/v1/tasks/{task}/history")
        assert history.status_code == 200 and len(history.json()["items"]) == 1
        parent = history.json()["items"][0]["version_id"]
        changed = {**base, "requirement": {"scenario": "采购培养设备，增加数据导出", "unknowns": []}, "expected_parent_version_id": parent}
        second = client.post(f"/api/v1/tasks/{task}/reports", json=changed)
        assert second.status_code == 200
        items = client.get(f"/api/v1/tasks/{task}/history").json()["items"]
        assert [item["version_number"] for item in items] == [1, 2]
        assert client.get(f"/api/v1/tasks/{task}/history/2").json()["changed_fields"] == ["report", "requirement"]
        assert client.get(f"/api/v1/tasks/{task}/history/diff?from_version=1&to_version=2").json()["requirement_changed"]


def test_task_list_and_report_list_only_count_generated_reports(tmp_path):
    app = create_app(database_path=tmp_path / "reports.sqlite3", storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购显微成像设备"}).json()
        task_id = created["task"]["id"]
        app.state.repository.put(
            "reports", "plan-test", {"kind": "search_plan", "plan_id": "plan-test"}, task_id=task_id,
        )

        before = client.get("/api/v1/tasks").json()["items"][0]
        assert before["id"] == task_id
        assert before["report_count"] == 0
        assert client.get(f"/api/v1/tasks/{task_id}/reports").json()["items"] == []

        response = client.post(f"/api/v1/tasks/{task_id}/reports", json={
            "requirement": created["requirement"],
            "candidates": [],
            "evaluations": [],
            "facts": [],
            "format": "json",
        })
        assert response.status_code == 200
        assert set(response.json()) == {"report", "export", "history_version"}

        reports = client.get(f"/api/v1/tasks/{task_id}/reports").json()["items"]
        assert len(reports) == 1
        assert reports[0]["report"]["report_id"] == response.json()["report"]["report_id"]
        after = client.get("/api/v1/tasks").json()["items"][0]
        assert after["report_count"] == 1
        assert after["version_count"] == 1

        updated = client.post(f"/api/v1/tasks/{task_id}/reports", json={
            "requirement": {**created["requirement"], "scenario": "采购显微成像设备并支持数据导出"},
            "candidates": [],
            "evaluations": [],
            "facts": [],
            "format": "json",
        })
        assert updated.status_code == 200
        assert updated.json()["history_version"]["version_number"] == 2
