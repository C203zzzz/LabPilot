from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.domain.errors import ModuleValidationError


class ConstraintKind(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class RequirementStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


@dataclass
class Constraint:
    name: str
    value: Any
    kind: ConstraintKind
    source_text: str = ""
    status: str = "extracted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind.value,
            "source_text": self.source_text,
            "status": self.status,
        }


@dataclass
class RequirementSpec:
    scenario: str
    task_type: str | None = None
    budget: dict[str, Any] | None = None
    throughput: dict[str, Any] | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    hard_constraints: list[Constraint] = field(default_factory=list)
    soft_constraints: list[Constraint] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    status: RequirementStatus = RequirementStatus.DRAFT
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "task_type": self.task_type,
            "budget": self.budget,
            "throughput": self.throughput,
            "environment": self.environment,
            "capabilities": self.capabilities,
            "hard_constraints": [x.to_dict() for x in self.hard_constraints],
            "soft_constraints": [x.to_dict() for x in self.soft_constraints],
            "unknowns": self.unknowns,
            "status": self.status.value,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RequirementSpec":
        if not isinstance(data, dict):
            raise ModuleValidationError("需求对象必须是 JSON 对象", code="invalid_requirement", field="requirement")

        def constraints(key: str, kind: ConstraintKind) -> list[Constraint]:
            result = []
            for item in data.get(key, []) or []:
                if isinstance(item, str):
                    result.append(Constraint(item, True, kind))
                elif isinstance(item, dict) and str(item.get("name") or "").strip():
                    result.append(Constraint(item["name"], item.get("value"), kind,
                                             item.get("source_text", ""), item.get("status", "edited")))
                else:
                    raise ModuleValidationError(f"{key} 中存在无效约束项", code="invalid_constraint", field=key)
            return result

        scenario = str(data.get("scenario", "")).strip()
        if not scenario:
            raise ModuleValidationError("scenario 不能为空", code="invalid_scenario", field="scenario")
        try:
            status = RequirementStatus(data.get("status", RequirementStatus.DRAFT.value))
        except ValueError as exc:
            raise ModuleValidationError("status 必须是 draft 或 confirmed", code="invalid_status", field="status") from exc
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("version 必须是正整数", code="invalid_version", field="version") from exc
        if version < 1:
            raise ModuleValidationError("version 必须是正整数", code="invalid_version", field="version")
        return cls(
            scenario=scenario,
            task_type=data.get("task_type"), budget=data.get("budget"),
            throughput=data.get("throughput"), environment=data.get("environment") or {},
            capabilities=list(data.get("capabilities") or []),
            hard_constraints=constraints("hard_constraints", ConstraintKind.HARD),
            soft_constraints=constraints("soft_constraints", ConstraintKind.SOFT),
            unknowns=list(data.get("unknowns") or []), status=status,
            version=version,
        )
