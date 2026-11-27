"""Defensive architecture, circular-import, and facade-parity tests after Phase 2/3 refactoring."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_refactored_modules_import_cleanly_without_cycles():
    modules = [
        "backend.api.routes",
        "backend.api.routes.schemas",
        "backend.api.routes.context",
        "backend.api.routes.providers",
        "backend.api.routes.tasks",
        "backend.api.routes.documents",
        "backend.api.routes.analysis",
        "backend.api.routes.search_candidates",
        "backend.api.routes.reports",
        "backend.api.routes.knowledge",
        "backend.services.knowledge_index",
        "backend.services.runtime_diagnostics",
        "backend.services.report_export_office",
        "backend.services.brand_model_rules",
        "backend.services.analysis_prompts",
        "backend.services.analysis_ownership",
        "backend.services.analysis_evidence_payload",
        "backend.services.deepseek_analysis",
        "backend.services.research.constants",
        "backend.services.research.price_helpers",
        "backend.services.research.report_helpers",
        "backend.services.research.candidate_gates",
        "backend.services.research.search_lane",
        "backend.services.research.content_fetcher",
        "backend.services.research.price_pipeline",
        "backend.services.research.candidate_pipeline",
        "backend.services.research.reporting",
        "backend.services.research.orchestrator",
        "backend.services.research",
        "backend.services.procurement_research",
    ]
    for name in modules:
        mod = importlib.import_module(name)
        assert mod is not None


def test_monolith_reduction_and_no_improved_duplicates():
    improved_files = [
        p for p in (ROOT / "backend").rglob("*_improved.py")
        if "__pycache__" not in p.parts
    ]
    assert improved_files == [], f"Found unexpected *_improved.py files: {improved_files}"

    deepseek_lines = len((ROOT / "backend/services/deepseek_analysis.py").read_text(encoding="utf-8").splitlines())
    pr_facade_lines = len((ROOT / "backend/services/procurement_research.py").read_text(encoding="utf-8").splitlines())
    routes_init_lines = len((ROOT / "backend/api/routes/__init__.py").read_text(encoding="utf-8").splitlines())

    assert deepseek_lines < 1000, f"deepseek_analysis.py grew back to {deepseek_lines} lines"
    assert pr_facade_lines < 300, f"procurement_research.py facade grew back to {pr_facade_lines} lines"
    assert routes_init_lines < 100, f"backend/api/routes/__init__.py grew to {routes_init_lines} lines"


def test_facade_monkeypatches_propagate_into_research_submodules(monkeypatch, tmp_path):
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services import deepseek_analysis, inventory as inventory_pkg, procurement_research
    from backend.services.official_search import OfficialSearchService
    from backend.services.research import constants as research_constants

    # 1. brand_identity on facade reaches report_helpers (brand_group_key)
    monkeypatch.setattr(procurement_research, "brand_identity", lambda v: f"patched:{v}")
    assert procurement_research.brand_group_key("Acme") == "patched:Acme"

    # 2. has_named_parameter_row on facade reaches candidate_gates
    page_text = "X100\n参数 | 数值\n转速 | 15000 rpm\n容量 | 24x1.5ml"
    monkeypatch.setattr(procurement_research, "has_named_parameter_row", lambda _text: False)
    cfg, _, _ = procurement_research.configuration_from_discovered_page(page_text, "X100")
    assert cfg is None

    # 3. OPERATION_TIMING_LIMIT on facade reaches orchestrator.record_operation
    repo = SQLiteRepository(tmp_path / "timings.sqlite3")
    service = procurement_research.ProcurementResearch(repo, OfficialSearchService(providers=[]), lambda: None)
    try:
        monkeypatch.setattr(procurement_research, "OPERATION_TIMING_LIMIT", 3)
        run = {"id": "r1", "task_id": "t1", "operation_timings": []}
        for idx in range(5):
            service.record_operation(run, f"op{idx}", 0.0, 0.0, "completed")
        assert len(run["operation_timings"]) == 3
        assert [x["operation"] for x in run["operation_timings"]] == ["op2", "op3", "op4"]
    finally:
        service.close()
        repo.close()

    # 4. LANES_TOGETHER facade attr helper reflects patch
    monkeypatch.setattr(procurement_research, "LANES_TOGETHER", False)
    assert research_constants.facade_attr("LANES_TOGETHER", True) is False

    # 5. deepseek_analysis._INVENTORY_BATCH_CHAR_LIMIT propagates to batch_builder during _run_inventory_stage
    monkeypatch.setattr(deepseek_analysis, "_INVENTORY_BATCH_CHAR_LIMIT", 1234)
    seen_limits = []

    class _FakeClient:
        def analyze(self, **_kwargs):
            seen_limits.append(inventory_pkg.batch_builder._INVENTORY_BATCH_CHAR_LIMIT)
            return {"candidates": []}

    svc = deepseek_analysis.DeepSeekAnalysisService(client=_FakeClient())
    svc._run_inventory_stage(
        "采购仪器",
        [{"document_id": "d1", "pages": [{"page": 1, "location": "page:1", "text": "Model X100"}]}],
        lambda **kw: svc.client.analyze(**kw),
        [],
    )
    assert seen_limits == [1234]


def test_create_app_route_surface_and_deepseek_patch_point(tmp_path):
    from backend.api import routes as routes_pkg
    from backend.main import create_app

    assert hasattr(routes_pkg, "DeepSeekClient")
    assert callable(routes_pkg.build_router)
    assert callable(routes_pkg.remove_task_owned_files)

    app = create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "files")
    try:
        paths = app.openapi().get("paths", {})
        ops_count = sum(len(v) for v in paths.values())
        assert len(paths) >= 82
        assert ops_count >= 94
    finally:
        app.state.research_service.close()
        app.state.repository.close()
