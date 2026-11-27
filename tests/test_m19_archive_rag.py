from fastapi.testclient import TestClient

from backend.domain.search import SearchResult
from backend.main import create_app
from backend.repositories.sqlite import SQLiteRepository
from backend.services import task_rag_service
from backend.services.official_search import FakeSearchProvider, OfficialSearchService

def test_m19_archive_rag_rebuild_query_and_restore_survive_reopen(tmp_path):
    db = tmp_path / "m19.sqlite3"
    storage = tmp_path / "storage"
    with TestClient(create_app(database_path=db, storage_dir=storage)) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购荧光酶标仪"}).json()["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "检测波长 450 nm；带温控", "text/plain")})
        rebuilt = client.post(f"/api/v1/tasks/{task_id}/rag/rebuild")
        assert rebuilt.status_code == 200 and rebuilt.json()["chunk_count"] >= 1
        hit = client.post(f"/api/v1/tasks/{task_id}/rag/query", json={"text": "450 nm"})
        assert hit.status_code == 200 and hit.json()["answerable"]
        assert hit.json()["items"][0]["chunk"]["location"]
        archive = client.post(f"/api/v1/tasks/{task_id}/archive", json={"note": "验收快照"})
        assert archive.status_code == 201
        payload = archive.json()
        assert payload["schema"] == "m19.task-archive.v1"
        snapshot = payload["snapshot"]
        for key in ("task", "requirement", "documents", "candidates", "confirmed_products", "reports", "search_runs", "source_decisions", "facts", "workflow_state", "comparisons", "questions", "supplier_replies", "final_confirmation", "history", "rag_chunks"):
            assert key in snapshot
        assert client.get(f"/api/v1/tasks/{task_id}/archive/read").json()["archive_id"] == payload["archive_id"]
    with TestClient(create_app(database_path=db, storage_dir=storage)) as reopened:
        assert reopened.get(f"/api/v1/tasks/{task_id}").status_code == 200
        assert reopened.post(f"/api/v1/tasks/{task_id}/rag/query", json={"text": "450 nm"}).json()["answerable"]
        restored = reopened.post(f"/api/v1/tasks/{task_id}/restore", json={}).json()
        assert restored["restored"]["documents"]


def test_m19_rag_excludes_search_snippets_and_model_prediction(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()["task"]["id"]
        uploaded = client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "真实正文", "text/plain")}).json()
        # Inject only through the repository fixture boundary to emulate M04
        repo = client.app.state.repository
        doc = repo.get("documents", uploaded["id"])
        doc["model_extraction"] = {"status": "completed", "fields": {"guess": "模型猜测"}}
        doc["extracted_text"] = "真实正文"
        repo.put("documents", uploaded["id"], doc, task_id=task_id)
        repo.put("search_runs", "run-1", {"id": "run-1", "sources": [{"url": "https://example.com", "body_status": "not_requested", "body_text": "摘要不应进入"}, {"url": "https://example.com/page", "body_status": "fetched", "body_text": "网页正文"}]}, task_id=task_id)
        result = client.post(f"/api/v1/tasks/{task_id}/rag/rebuild").json()
        texts = " ".join(chunk["text"] for chunk in result["chunks"])
        assert "摘要不应进入" not in texts and "模型猜测" not in texts and "网页正文" in texts


def test_m19_restore_keeps_m18_scoped_records(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()["task"]["id"]
        decision = client.put(f"/api/v1/tasks/{task_id}/final-decision", json={
            "status": "confirmed", "explicit_user_confirmation": True, "decided_by": "user-1"
        })
        assert decision.status_code == 200
        archive = client.post(f"/api/v1/tasks/{task_id}/archive").json()
        assert client.post(f"/api/v1/tasks/{task_id}/restore", json={"archive_id": archive["archive_id"]}).status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}/final-decision").json()["status"] == "confirmed"


DIRTY_BODY = ('某学院实验室仪器采购项目成交公告\n'
              '规格型号 甲厂Q10，单价 74480.0 元，成交金额 74480.0 元\n'
              '供应商联系人：张三，联系电话 13800000000\n'
              '项目编号 2251451000001337350')


def test_m04_search_run_body_is_masked_before_it_lands(tmp_path):
    """M04 那条独立车道（``POST /tasks/{id}/search`` → ``search_runs`` 表）。

    它不在 ``procurement_research`` 里，抓正文走 ``official_search``，所以共享
    落盘点覆盖不到（``设计规范`` §4 第 13 行）。这里放一个**替身 provider**：
    替身返回的正文不保证经过产品自己的那段代码，所以路由在 ``repo.put`` 之前
    还有一道兜底——这一条钉的就是"正文最终落到表里时一定干净"。
    """
    url = "https://lab-supplier.example.edu.cn/award/2026-0003.html"
    query = "甲厂Q10 成交公告"
    provider = FakeSearchProvider({query: [SearchResult(url, "成交公告", "摘要")]},
                                  {url: DIRTY_BODY})
    search = OfficialSearchService(providers=[provider], fetch_body=True)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3",
                               storage_dir=tmp_path / "storage", search_service=search)) as client:
        created = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()
        task_id = created["task"]["id"]
        # 这条车道要求需求已确认（否则 409），照它自己的流程走一遍。
        assert client.post(f"/api/v1/tasks/{task_id}/requirements/confirm",
                           json={"requirement": created["requirement"]}).status_code == 200
        result = client.post(f"/api/v1/tasks/{task_id}/search",
                             json={"query": query, "purpose": "price", "fetch_body": True})
        assert result.status_code == 200
        sources = result.json()["items"][0]["sources"]
        assert [s["body_status"] for s in sources] == ["fetched"]
        body = sources[0]["body_text"]
        assert "13800000000" not in body and "***" in body
        for kept in ("74480.0", "甲厂Q10", "2251451000001337350"):
            assert kept in body, kept
        # 库里那一份（不是响应里的那一份）同样干净。
        stored = client.app.state.repository.list("search_runs", task_id)[0]
        assert "13800000000" not in stored["sources"][0]["body_text"]


def test_the_rag_copy_of_a_search_run_body_is_masked_too(tmp_path):
    """RAG 表是 ``search_runs`` 正文的**另一份副本**，也要过同一道掩码。

    ``设计规范`` §4 第 14 行。这里的 ``search_runs`` 记录是**直接写进仓库**的旧
    脏记录（模拟修复之前落盘的数据），重建 RAG 之后 ``rag_chunks`` 里不许再有号码
    ——历史数据本身不动（业务口径），但"再抄一份进 RAG"这一步不能把脏的抄进去。
    """
    repo = SQLiteRepository(tmp_path / "app.sqlite3")
    try:
        repo.put("tasks", "task", {"id": "task"})
        repo.put("search_runs", "run-dirty", {
            "id": "run-dirty",
            "sources": [{"url": "https://lab-supplier.example.edu.cn/award/old.html",
                         "body_status": "fetched", "source_type": "unknown",
                         "body_text": DIRTY_BODY}]}, task_id="task")
        result = task_rag_service.rebuild(repo, "task")
        texts = " ".join(chunk["text"] for chunk in result["chunks"])
        assert "13800000000" not in texts and "***" in texts
        assert "74480.0" in texts and "甲厂Q10" in texts
        stored = " ".join(row.get("text", "") for row in repo.list_rag_chunks("task"))
        assert "13800000000" not in stored and "74480.0" in stored
    finally:
        repo.close()
