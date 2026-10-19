"""M12 task history contracts.

Task versions are immutable, task-scoped snapshots that link the M01
requirement and M03 candidates to one M11 report version.  The repository is
intentionally in-memory; persistence belongs to M15.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import copy
import json
from typing import Any, Protocol, runtime_checkable

from backend.domain.errors import ModuleValidationError
from backend.domain.reports import ReportSnapshot


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModuleValidationError(f"{field} is required", code="invalid_input", field=field)
    return value.strip()


def _primitive(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _primitive(value.to_dict())
    if isinstance(value, dict):
        return {str(k): _primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class TaskVersion:
    task_id: str
    version_number: int
    parent_version_id: str | None
    revision_reason: str
    created_at: datetime
    requirement: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    report_version_id: str
    report_version_number: int
    report_content_hash: str
    changed_fields: tuple[str, ...] = ()
    content_hash: str = ""
    version_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "revision_reason", _text(self.revision_reason, "revision_reason"))
        object.__setattr__(self, "report_version_id", _text(self.report_version_id, "report_version_id"))
        object.__setattr__(self, "report_content_hash", _text(self.report_content_hash, "report_content_hash"))
        try:
            number = int(self.version_number)
            report_number = int(self.report_version_number)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("version numbers must be positive integers", code="invalid_version", field="version_number") from exc
        if number < 1 or report_number < 1:
            raise ModuleValidationError("version numbers must be positive integers", code="invalid_version", field="version_number")
        object.__setattr__(self, "version_number", number)
        object.__setattr__(self, "report_version_number", report_number)
        parent = self.parent_version_id.strip() if isinstance(self.parent_version_id, str) and self.parent_version_id.strip() else None
        object.__setattr__(self, "parent_version_id", parent)
        if isinstance(self.created_at, str):
            try:
                object.__setattr__(self, "created_at", datetime.fromisoformat(self.created_at))
            except ValueError as exc:
                raise ModuleValidationError("created_at must be ISO-8601", code="invalid_created_at", field="created_at") from exc
        if not isinstance(self.created_at, datetime):
            raise ModuleValidationError("created_at must be datetime", code="invalid_created_at", field="created_at")
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.requirement, dict):
            raise ModuleValidationError("requirement must be an object", code="invalid_input", field="requirement")
        object.__setattr__(self, "requirement", copy.deepcopy(self.requirement))
        normalized = tuple(copy.deepcopy(dict(item)) for item in self.candidates if isinstance(item, dict))
        if len(normalized) != len(self.candidates):
            raise ModuleValidationError("candidates must contain objects", code="invalid_input", field="candidates")
        object.__setattr__(self, "candidates", normalized)
        object.__setattr__(self, "changed_fields", tuple(sorted(set(str(v) for v in self.changed_fields))))
        object.__setattr__(self, "content_hash", self.content_hash or sha256(_canonical({
            "task_id": self.task_id, "requirement": self.requirement,
            "candidates": self.candidates, "report_version_id": self.report_version_id,
            "report_version_number": self.report_version_number, "report_content_hash": self.report_content_hash,
        }).encode()).hexdigest())
        object.__setattr__(self, "version_id", self.version_id or "task-version-" + self.content_hash[:16])

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "version_number": self.version_number, "parent_version_id": self.parent_version_id,
                "revision_reason": self.revision_reason, "created_at": self.created_at.isoformat(),
                "requirement": copy.deepcopy(self.requirement), "candidates": copy.deepcopy(list(self.candidates)),
                "report_version_id": self.report_version_id, "report_version_number": self.report_version_number,
                "report_content_hash": self.report_content_hash, "changed_fields": list(self.changed_fields),
                "content_hash": self.content_hash, "version_id": self.version_id}

    @property
    def requirement_version(self) -> Any:
        """The upstream M01 version when the requirement carries one."""
        return self.requirement.get("version")

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(str(item.get("id") or item.get("candidate_id")) for item in self.candidates)

    @property
    def report_version(self) -> str:
        return self.report_version_id


@dataclass(frozen=True)
class TaskVersionDiff:
    task_id: str
    from_version_id: str | None
    to_version_id: str
    changed_fields: tuple[str, ...]
    requirement_changed: bool
    candidates_added: tuple[str, ...]
    candidates_removed: tuple[str, ...]
    candidates_changed: tuple[str, ...]
    report_version_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "from_version_id": self.from_version_id, "to_version_id": self.to_version_id,
                "changed_fields": list(self.changed_fields), "requirement_changed": self.requirement_changed,
                "candidates_added": list(self.candidates_added), "candidates_removed": list(self.candidates_removed),
                "candidates_changed": list(self.candidates_changed), "report_version_changed": self.report_version_changed}


@runtime_checkable
class TaskHistoryRepository(Protocol):
    def save(self, version: TaskVersion, expected_parent_version_id: str | None = None) -> TaskVersion: ...
    def get(self, task_id: str, version_number: int) -> TaskVersion | None: ...
    def list_versions(self, task_id: str) -> list[TaskVersion]: ...
    def latest(self, task_id: str) -> TaskVersion | None: ...


def _copy(version: TaskVersion) -> TaskVersion:
    return TaskVersion(**version.to_dict())


class InMemoryTaskHistoryRepository:
    def __init__(self) -> None:
        self._versions: dict[str, list[TaskVersion]] = {}

    def save(self, version: TaskVersion, expected_parent_version_id: str | None = None) -> TaskVersion:
        if not isinstance(version, TaskVersion):
            raise ModuleValidationError("version must be TaskVersion", code="invalid_version", field="version")
        versions = self._versions.setdefault(version.task_id, [])
        if not versions:
            if expected_parent_version_id is not None or version.parent_version_id is not None or version.version_number != 1:
                raise ModuleValidationError("first task version has invalid parent", code="version_conflict", field="parent_version_id")
        else:
            current = versions[-1]
            if current.content_hash == version.content_hash:
                if expected_parent_version_id not in (None, current.version_id):
                    raise ModuleValidationError("parent version is stale", code="version_conflict", field="parent_version_id")
                return _copy(current)
            if expected_parent_version_id != current.version_id or version.parent_version_id != current.version_id:
                raise ModuleValidationError("parent version is stale", code="version_conflict", field="parent_version_id")
            if version.version_number != current.version_number + 1:
                raise ModuleValidationError("task version must increase by one", code="version_conflict", field="version_number")
        versions.append(_copy(version))
        return _copy(version)

    def get(self, task_id: str, version_number: int) -> TaskVersion | None:
        _text(task_id, "task_id")
        return next((_copy(v) for v in self._versions.get(task_id, []) if v.version_number == int(version_number)), None)

    def list_versions(self, task_id: str) -> list[TaskVersion]:
        _text(task_id, "task_id")
        return [_copy(v) for v in self._versions.get(task_id, [])]

    def latest(self, task_id: str) -> TaskVersion | None:
        _text(task_id, "task_id")
        values = self._versions.get(task_id, [])
        return _copy(values[-1]) if values else None


__all__ = ["TaskVersion", "TaskVersionDiff", "TaskHistoryRepository", "InMemoryTaskHistoryRepository"]
