"""Phase 4.1 tests: pure-SQL cross-task knowledge index, hash dedup, and supplier comparison."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app


def _build_app(tmp_path: Path):
    return create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")


def test_knowledge_index_uses_separate_db_and_dedups_by_hash_and_exact_model(tmp_path):
    app = _build_app(tmp_path)
    with TestClient(app) as client:
        repo = app.state.repository
        kindex = app.state.knowledge_index
        assert kindex.path == tmp_path / "knowledge_index.sqlite3"
        assert len(set(repo.task_scoped_tables())) == 18

        # Seed two tasks sharing identical document SHA-256 and close models (SW-CJ-1FD vs SW-CJ-2FD)
        for idx, (tid, model, price, supplier, warranty) in enumerate(
            [
                ("task-k1", "SW-CJ-1FD", 5800.0, "华东科仪", "2年质保"),
                ("task-k2", "SW-CJ-1FD", 5200.0, "博雅实验", "3年整机质保"),
                ("task-k3", "SW-CJ-2FD", 9800.0, "华东科仪", "2年质保"),
            ],
            1,
        ):
            repo.put("tasks", tid, {"id": tid, "scenario": f"采购超净台{idx}", "category": "超净工作台", "status": "draft"})
            repo.put("requirements", tid, {"category": "超净工作台", "scenario": f"采购超净台{idx}"})
            repo.put(
                "documents",
                f"doc-{tid}",
                {
                    "id": f"doc-{tid}",
                    "source_name": "彩页.pdf",
                    "sha256": "a" * 64 if idx in {1, 2} else "b" * 64,
                    "extracted_text": f"品牌 DRAWELL 型号 {model} 垂直层流超净工作台",
                },
                task_id=tid,
            )
            repo.put(
                "procurement_records",
                f"base-{tid}",
                {
                    "id": f"base-{tid}",
                    "kind": "baseline",
                    "brand": "DRAWELL",
                    "model": model,
                    "category": "超净工作台",
                    "supplier": supplier,
                    "supplier_price": price,
                    "currency": "CNY",
                    "quantity": 1,
                    "warranty": warranty,
                    "created_at": f"2026-10-0{idx}T10:00:00+00:00",
                },
                task_id=tid,
            )
            repo.put(
                "procurement_records",
                f"run-{tid}",
                {
                    "id": f"run-{tid}",
                    "kind": "research",
                    "baseline": {"brand": "DRAWELL", "model": model, "category": "超净工作台"},
                    "prices": [
                        {
                            "brand": "DRAWELL",
                            "model": model,
                            "unit_price": price - 300.0,
                            "currency": "CNY",
                            "quantity": 1,
                            "date": f"2026-09-1{idx}",
                            "supplier": f"{supplier}-中标",
                            "url": f"https://www.ccgp.gov.cn/notice/{tid}.htm",
                            "excerpt": f"型号 {model} 成交单价 {price - 300.0}",
                        }
                    ],
                    "alternatives": [],
                    "created_at": f"2026-10-0{idx}T12:00:00+00:00",
                },
                task_id=tid,
            )

        res = client.post("/api/v1/knowledge/reindex")
        assert res.status_code == 200
        body = res.json()
        assert body["synced"]["tasks_synced"] == 3
        assert body["synced"]["documents_indexed"] == 2
        assert body["synced"]["documents_deduped"] == 1
        assert body["synced"]["quotes_indexed"] == 6

        # Exact model hard filter must keep SW-CJ-1FD completely separate from SW-CJ-2FD
        q_res = client.get("/api/v1/knowledge/quotes", params={"model": "sw cj 1fd", "exact_model": True})
        assert q_res.status_code == 200
        q_data = q_res.json()
        assert q_data["total"] == 4
        assert {item["model"] for item in q_data["items"]} == {"SW-CJ-1FD"}
        assert q_data["stats"]["min_price"] == 4900.0
        assert q_data["stats"]["max_price"] == 5800.0

        # Supplier horizontal comparison
        cmp_res = client.get("/api/v1/knowledge/compare", params={"model": "SW-CJ-1FD", "exact_model": True})
        assert cmp_res.status_code == 200
        cmp_data = cmp_res.json()
        assert cmp_data["total"] == 4
        assert cmp_data["items"][0]["median_price"] == 4900.0

        # Cross-task document search
        doc_res = client.get("/api/v1/knowledge/documents", params={"keyword": "超净工作台"})
        assert doc_res.status_code == 200
        assert doc_res.json()["total"] == 2

        # Deleting a task also cleans its index rows
        del_res = client.post("/api/v1/tasks/delete", json={"task_ids": ["task-k3"]})
        assert del_res.status_code == 200
        after_q = client.get("/api/v1/knowledge/quotes", params={"model": "SW-CJ-2FD", "exact_model": True}).json()
        assert after_q["total"] == 0


def test_knowledge_quotes_can_join_external_xlyg_sqlite_with_exact_model_filter(tmp_path):
    xlyg_db = tmp_path / "xlyg_prices.sqlite3"
    con = sqlite3.connect(xlyg_db)
    con.executescript("""
    CREATE TABLE xlyg_item (
        order_no TEXT, line_no INTEGER, goods_name TEXT, product_name TEXT,
        brand TEXT, model TEXT, quantity REAL, unit_price REAL,
        category TEXT, deal_date TEXT, purchaser TEXT, supplier TEXT,
        detail_url TEXT, excerpt TEXT, verified INTEGER
    );
    INSERT INTO xlyg_item VALUES ('ORD-1', 1, '离心机', '离心机', '湘仪', 'H1850R', 1, 16800, '离心机', '2026-08-01', '某学院', '广州科教', 'https://xlyg.example/1', 'H1850R 16800', 1);
    INSERT INTO xlyg_item VALUES ('ORD-2', 1, '离心机', '离心机', '湘仪', 'H1850', 1, 11800, '离心机', '2026-08-02', '某学院', '广州科教', 'https://xlyg.example/2', 'H1850 11800', 1);
    """)
    con.commit()
    con.close()

    app = create_app(
        database_path=tmp_path / "app.sqlite3",
        storage_dir=tmp_path / "files",
        xlyg_database_path=xlyg_db,
    )
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/knowledge/quotes",
            params={"model": "H1850R", "exact_model": True, "include_xlyg": True},
        )
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["model"] == "H1850R"
        assert items[0]["source_kind"] == "xlyg_award"
        assert items[0]["unit_price"] == 16800.0
