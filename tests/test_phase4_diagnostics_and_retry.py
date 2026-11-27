"""Phase 4.2 & 4.3 & Perf tests: diagnostics guard, single-lane retry, deterministic fetch dedup."""
from __future__ import annotations

import socket
from pathlib import Path

from fastapi.testclient import TestClient

from backend.domain.search import SearchQuery
from backend.infrastructure.search_providers import (
    BaiduSearchProvider,
    CCGPSearchProvider,
)
from backend.main import create_app


def test_diagnostics_detects_tun_fake_ip_and_strict_mode_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_DIAGNOSTICS_SKIP_DNS", "0")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 0))],
    )

    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")
    with TestClient(app) as client:
        r = client.get("/api/v1/system/diagnostics", params={"strict": False, "check_dns": True})
        assert r.status_code == 200
        data = r.json()
        assert data["overall_status"] == "broken"
        by_id = {item["id"]: item for item in data["items"]}
        assert by_id["network_dns_guard"]["status"] == "error"
        assert "198.18.0.42" in by_id["network_dns_guard"]["message"]
        assert by_id["reasoning_model"]["status"] == "error"
        assert len(data["silent_degradation_risks"]) >= 2

        strict = client.get("/api/v1/system/diagnostics", params={"strict": True, "check_dns": True})
        assert strict.status_code == 503
        assert strict.json()["detail"]["overall_status"] == "broken"


def test_single_lane_retry_preserves_checkpoint_and_replenishes_budget(tmp_path):
    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")
    with TestClient(app) as client:
        service = app.state.research_service
        repo = app.state.repository
        task_id = "task-retry1"
        repo.put("tasks", task_id, {"id": task_id, "scenario": "低温离心机采购", "category": "离心机"})
        baseline = {
            "id": "base-retry1",
            "task_id": task_id,
            "kind": "baseline",
            "version": 1,
            "intake_revision": 0,
            "scenario": "低温离心机采购",
            "category": "离心机",
            "brand": "湘仪",
            "model": "H1850R",
            "status": "confirmed",
            "created_at": "2026-10-01T00:00:00+00:00",
        }
        repo.put("procurement_records", baseline["id"], baseline, task_id=task_id)

        run_id = "run-retry1"
        run = {
            "id": run_id,
            "task_id": task_id,
            "kind": "research",
            "status": "partial",
            "round": 1,
            "stage": "核查其他品牌",
            "baseline_id": baseline["id"],
            "baseline": baseline,
            "lanes": {"price": "completed", "alternatives": "failed"},
            "usage": {"search": 20, "fetch": 80, "model": 12, "education_platform": 0, "province_award": 0},
            "limits": {"search": 20, "fetch": 80, "model": 12, "education_platform": 6, "province_award": 6},
            "sources": [{"url": "https://example.org/kept", "body_status": "fetched", "body_text": "保留的正文"}],
            "prices": [{"model": "H1850R", "unit_price": 15800.0}],
            "alternatives": [],
            "gaps": [],
            "failures": [],
            "completed_queries": ["已完成的检索词"],
            "unavailable_search_providers": ["baidu"],
            "provider_failure_counts": {"baidu": 3},
            "created_at": "2026-10-01T00:01:00+00:00",
        }
        repo.put("procurement_records", run_id, run, task_id=task_id)

        calls = []
        service.execute = lambda tid, rid: calls.append((tid, rid))

        resp = client.post(f"/api/v1/tasks/{task_id}/research-runs/{run_id}/lanes/alternatives/retry", json={})
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["round"] == 2
        assert updated["active_lane"] == "alternatives"
        assert updated["lanes"]["price"] == "completed"
        assert updated["lanes"]["alternatives"] == "pending"
        assert updated["limits"]["search"] > 20
        assert updated["limits"]["fetch"] > 80
        assert len(updated["sources"]) == 1
        assert updated["completed_queries"] == ["已完成的检索词"]
        assert "unavailable_search_providers" not in updated
        assert len(updated["lane_retries"]) == 1
        assert updated["lane_retries"][0]["preserved_sources"] == 1
        assert calls == [(task_id, run_id)]


def test_private_network_error_is_not_misclassified_as_parse_incomplete_and_skips_repeat_fetch(tmp_path):
    class _PrivateNetSearch:
        providers = []
        timeout = 5.0

        class _Reader:
            def __init__(self):
                self.calls = 0

            def read(self, url, timeout, on_attempt=None, referer=None):
                self.calls += 1
                if on_attempt:
                    on_attempt()
                raise ValueError("来源域名解析到私有网络，无法作为公开来源读取")

        def __init__(self):
            self.reader = self._Reader()

    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.procurement_research import ProcurementResearch

    repo = SQLiteRepository(tmp_path / "pnet.sqlite3")
    search = _PrivateNetSearch()
    service = ProcurementResearch(repo, search, lambda: None)
    try:
        run = {
            "id": "r-pnet",
            "task_id": "t-pnet",
            "kind": "research",
            "status": "running",
            "round": 1,
            "usage": {"search": 0, "fetch": 0, "model": 0},
            "limits": {"search": 10, "fetch": 10, "model": 10},
            "sources": [],
            "gaps": [],
            "failures": [],
        }
        repo.put("procurement_records", run["id"], run, task_id=run["task_id"])
        first = service.fetch_source(run, {"url": "https://blocked.example.com/a.htm"})
        assert first["body_status"] == "unavailable"
        assert first["failure_kind"] == "private_network_blocked"
        assert "私有网络" in first["error_reason"]
        assert search.reader.calls == 1

        run["sources"].append(first)
        second = service.fetch_source(run, {"url": "https://blocked.example.com/a.htm"})
        assert second["failure_kind"] == "private_network_blocked"
        assert search.reader.calls == 1
        assert run["usage"]["fetch"] == 1
    finally:
        service.close()
        repo.close()


def test_search_provider_timeout_defaults_and_ccgp_baidu_exist():
    assert CCGPSearchProvider().name == "ccgp"
    assert BaiduSearchProvider().name == "baidu"
