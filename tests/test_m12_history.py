import pytest

from backend.domain.errors import ModuleValidationError
from backend.domain.history import InMemoryTaskHistoryRepository
from backend.services.history_service import TaskHistoryService
from tests.test_m11_reports import base_inputs
from backend.services.report_service import ReportService


def report(task_id="task-a"):
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    return ReportService().create_version(task_id, requirement, candidates, evaluations, facts, ranking=ranking)


def test_task_history_idempotency_diff_and_isolation():
    snapshot = report()
    service = TaskHistoryService()
    requirement, candidates, *_ = base_inputs()
    v1 = service.create_version("task-a", requirement, candidates, snapshot, revision_reason="initial", created_at="2026-09-18T10:00:00+08:00")
    same = service.create_version("task-a", requirement, candidates, snapshot, revision_reason="ignored", created_at="2026-09-18T11:00:00+08:00")
    assert same.version_id == v1.version_id
    changed = dict(requirement)
    changed["scenario"] = "变更后的采购场景"
    candidates2 = list(candidates) + [{**candidates[0], "id": "c3", "model": "Z-1"}]
    v2 = service.create_version("task-a", changed, candidates2, snapshot, expected_parent_version_id=v1.version_id)
    diff = service.diff("task-a", 1, 2)
    assert diff.requirement_changed and diff.candidates_added == ("c3",)
    assert diff.changed_fields == ("requirement", "candidates")
    assert service.history("task-b") == []
    history = service.get_version("task-a", 1)
    history.requirement["scenario"] = "污染尝试"
    assert service.get_version("task-a", 1).requirement["scenario"] != "污染尝试"


def test_task_history_rejects_cross_task_and_stale_parent():
    requirement, candidates, *_ = base_inputs()
    service = TaskHistoryService(InMemoryTaskHistoryRepository())
    with pytest.raises(ModuleValidationError) as mismatch:
        service.create_version("task-b", requirement, candidates, report("task-a"))
    assert mismatch.value.code == "association_mismatch"
    snap = report("task-a")
    v1 = service.create_version("task-a", requirement, candidates, snap)
    with pytest.raises(ModuleValidationError) as stale:
        service.create_version("task-a", {**requirement, "scenario": "new"}, candidates, snap, expected_parent_version_id="stale")
    assert stale.value.code == "version_conflict"
    assert service.latest("task-a").version_id == v1.version_id
