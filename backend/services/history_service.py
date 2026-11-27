"""M12 task history and stable version-diff orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import copy

from backend.domain.errors import ModuleValidationError
from backend.domain.history import InMemoryTaskHistoryRepository, TaskHistoryRepository, TaskVersion, TaskVersionDiff
from backend.domain.reports import ReportSnapshot


def _copy_data(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return copy.deepcopy(value.to_dict())
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, (list, tuple)):
        return copy.deepcopy(list(value))
    raise ModuleValidationError("value must be an object or list", code="invalid_input")


class TaskHistoryService:
    def __init__(self, repository: TaskHistoryRepository | None = None) -> None:
        self.repository = repository or InMemoryTaskHistoryRepository()

    def create_version(self, task_id: str, requirement: Any, candidates: Any, report_snapshot: ReportSnapshot,
                       *, revision_reason: str | None = None, created_at: datetime | str | None = None,
                       expected_parent_version_id: str | None = None) -> TaskVersion:
        if not isinstance(report_snapshot, ReportSnapshot):
            raise ModuleValidationError("report_snapshot must be ReportSnapshot", code="invalid_snapshot", field="report_snapshot")
        if report_snapshot.task_id != task_id:
            raise ModuleValidationError("task and report task_id mismatch", code="association_mismatch", field="task_id")
        requirement_data = _copy_data(requirement)
        if not isinstance(requirement_data, dict):
            raise ModuleValidationError("requirement must be an object", code="invalid_input", field="requirement")
        candidates_data = _copy_data(candidates)
        if not isinstance(candidates_data, list):
            raise ModuleValidationError("candidates must be a list", code="invalid_input", field="candidates")
        candidate_ids = []
        for candidate in candidates_data:
            if not isinstance(candidate, dict) or not (candidate.get("id") or candidate.get("candidate_id")):
                raise ModuleValidationError("candidate id is required", code="invalid_input", field="candidates")
            candidate_ids.append(str(candidate.get("id") or candidate.get("candidate_id")))
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ModuleValidationError("duplicate candidate id", code="invalid_input", field="candidates")
        latest = self.repository.latest(task_id)
        candidate_map = {str(item.get("id") or item.get("candidate_id")): item for item in candidates_data}
        requirement_changed = latest is None or latest.requirement != requirement_data
        previous_map = {} if latest is None else {str(item.get("id") or item.get("candidate_id")): item for item in latest.candidates}
        added = sorted(set(candidate_map) - set(previous_map))
        removed = sorted(set(previous_map) - set(candidate_map))
        changed = sorted(key for key in set(candidate_map) & set(previous_map) if candidate_map[key] != previous_map[key])
        report_changed = latest is None or latest.report_content_hash != report_snapshot.content_hash
        fields = []
        if requirement_changed: fields.append("requirement")
        if added or removed or changed: fields.append("candidates")
        if report_changed: fields.append("report")
        if created_at is None: created_at = datetime.now(timezone.utc)
        elif isinstance(created_at, str):
            try: created_at = datetime.fromisoformat(created_at)
            except ValueError as exc: raise ModuleValidationError("created_at must be ISO-8601", code="invalid_created_at", field="created_at") from exc
        if not isinstance(created_at, datetime):
            raise ModuleValidationError("created_at must be datetime", code="invalid_created_at", field="created_at")
        if created_at.tzinfo is None: created_at = created_at.replace(tzinfo=timezone.utc)
        version = TaskVersion(task_id=task_id, version_number=1 if latest is None else latest.version_number + 1,
                              parent_version_id=None if latest is None else latest.version_id,
                              revision_reason=revision_reason or ("initial" if latest is None else "input_changed"),
                              created_at=created_at, requirement=requirement_data, candidates=tuple(candidates_data),
                              report_version_id=report_snapshot.version_id, report_version_number=report_snapshot.version_number,
                              report_content_hash=report_snapshot.content_hash, changed_fields=tuple(fields))
        return self.repository.save(version, expected_parent_version_id)

    create = create_version

    def history(self, task_id: str) -> list[TaskVersion]: return self.repository.list_versions(task_id)
    list_versions = history
    def get_version(self, task_id: str, version_number: int) -> TaskVersion | None: return self.repository.get(task_id, version_number)
    def latest(self, task_id: str) -> TaskVersion | None: return self.repository.latest(task_id)

    def diff(self, task_id: str, from_version: int | str | None, to_version: int | str | None = None) -> TaskVersionDiff:
        target = self.latest(task_id) if to_version is None else self._resolve(task_id, to_version)
        if target is None: raise ModuleValidationError("target version not found", code="version_not_found", field="to_version")
        source = None if from_version is None else self._resolve(task_id, from_version)
        if from_version is not None and source is None: raise ModuleValidationError("source version not found", code="version_not_found", field="from_version")
        old = {} if source is None else {str(i.get("id") or i.get("candidate_id")): i for i in source.candidates}
        new = {str(i.get("id") or i.get("candidate_id")): i for i in target.candidates}
        req_changed = source is None or source.requirement != target.requirement
        added, removed = sorted(set(new)-set(old)), sorted(set(old)-set(new))
        changed = sorted(k for k in set(old)&set(new) if old[k] != new[k])
        fields = []
        if req_changed: fields.append("requirement")
        if added or removed or changed: fields.append("candidates")
        if source is None or source.report_content_hash != target.report_content_hash: fields.append("report")
        return TaskVersionDiff(task_id, None if source is None else source.version_id, target.version_id, tuple(fields), req_changed, tuple(added), tuple(removed), tuple(changed), source is None or source.report_content_hash != target.report_content_hash)

    def _resolve(self, task_id: str, version: int | str) -> TaskVersion | None:
        try: return self.repository.get(task_id, int(version))
        except (TypeError, ValueError): return next((v for v in self.history(task_id) if v.version_id == str(version)), None)


__all__ = ["TaskHistoryService"]
