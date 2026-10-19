"""第一性原理优化：requirements领域模型

修复P0.1：不可变性
修复P0.2：输入校验
修复P0.3：状态转换保护
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class ConstraintKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class Constraint:
    """不可变的约束对象"""
    name: str
    value: str | int | float | dict | list  # 明确类型，不用Any
    kind: ConstraintKind
    source_text: str = ""
    status: str = "extracted"

    def __post_init__(self):
        """输入校验"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("constraint name cannot be empty")
        if not isinstance(self.kind, ConstraintKind):
            raise ValueError(f"kind must be ConstraintKind, got {type(self.kind)}")
        # 确保value类型正确
        if not isinstance(self.value, (str, int, float, dict, list, bool)):
            raise ValueError(f"value type not supported: {type(self.value)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind.value,
            "source_text": self.source_text,
            "status": self.status,
        }


@dataclass(frozen=True)
class RequirementSpec:
    """不可变的需求规格对象"""
    scenario: str
    task_type: str | None = None
    budget: dict[str, Any] | None = None
    throughput: dict[str, Any] | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()  # 改为tuple
    hard_constraints: tuple[Constraint, ...] = ()  # 改为tuple
    soft_constraints: tuple[Constraint, ...] = ()  # 改为tuple
    unknowns: tuple[str, ...] = ()  # 改为tuple
    status: RequirementStatus = RequirementStatus.DRAFT
    version: int = 1

    def __post_init__(self):
        """输入校验"""
        if not isinstance(self.scenario, str) or not self.scenario.strip():
            raise ValueError("scenario cannot be empty")
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version}")
        if not isinstance(self.status, RequirementStatus):
            raise ValueError(f"status must be RequirementStatus, got {type(self.status)}")

        # 转换可变容器为不可变
        if isinstance(self.capabilities, list):
            object.__setattr__(self, 'capabilities', tuple(self.capabilities))
        if isinstance(self.hard_constraints, list):
            object.__setattr__(self, 'hard_constraints', tuple(self.hard_constraints))
        if isinstance(self.soft_constraints, list):
            object.__setattr__(self, 'soft_constraints', tuple(self.soft_constraints))
        if isinstance(self.unknowns, list):
            object.__setattr__(self, 'unknowns', tuple(self.unknowns))

        # 校验budget
        if self.budget is not None:
            if not isinstance(self.budget, dict):
                raise ValueError("budget must be dict")
            if 'max_amount' in self.budget:
                amount = self.budget['max_amount']
                if not isinstance(amount, (int, float)) or amount <= 0:
                    raise ValueError(f"budget max_amount must be positive number, got {amount}")

        # 校验throughput
        if self.throughput is not None:
            if not isinstance(self.throughput, dict):
                raise ValueError("throughput must be dict")
            for key in ('per_day', 'per_hour', 'per_run'):
                if key in self.throughput:
                    value = self.throughput[key]
                    if not isinstance(value, (int, float)) or value <= 0:
                        raise ValueError(f"throughput {key} must be positive number, got {value}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "task_type": self.task_type,
            "budget": self.budget,
            "throughput": self.throughput,
            "environment": dict(self.environment),  # 深拷贝
            "capabilities": list(self.capabilities),
            "hard_constraints": [x.to_dict() for x in self.hard_constraints],
            "soft_constraints": [x.to_dict() for x in self.soft_constraints],
            "unknowns": list(self.unknowns),
            "status": self.status.value,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequirementSpec:
        """从字典创建，带完整校验"""
        from backend.domain.errors import ModuleValidationError

        if not isinstance(data, dict):
            raise ModuleValidationError(
                "需求对象必须是 JSON 对象",
                code="invalid_requirement",
                field="requirement"
            )

        # 校验scenario
        scenario = str(data.get("scenario", "")).strip()
        if not scenario:
            raise ModuleValidationError(
                "scenario 不能为空",
                code="invalid_scenario",
                field="scenario"
            )

        # 校验status
        try:
            status = RequirementStatus(data.get("status", RequirementStatus.DRAFT.value))
        except ValueError as exc:
            raise ModuleValidationError(
                "status 必须是 draft 或 confirmed",
                code="invalid_status",
                field="status"
            ) from exc

        # 校验version
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError(
                "version 必须是正整数",
                code="invalid_version",
                field="version"
            ) from exc
        if version < 1:
            raise ModuleValidationError(
                "version 必须是正整数",
                code="invalid_version",
                field="version"
            )

        # 解析约束
        def parse_constraints(key: str, kind: ConstraintKind) -> tuple[Constraint, ...]:
            result = []
            for item in data.get(key, []) or []:
                if isinstance(item, str):
                    result.append(Constraint(item, True, kind))
                elif isinstance(item, dict) and str(item.get("name") or "").strip():
                    result.append(
                        Constraint(
                            item["name"],
                            item.get("value"),
                            kind,
                            item.get("source_text", ""),
                            item.get("status", "edited")
                        )
                    )
                else:
                    raise ModuleValidationError(
                        f"{key} 中存在无效约束项",
                        code="invalid_constraint",
                        field=key
                    )
            return tuple(result)

        return cls(
            scenario=scenario,
            task_type=data.get("task_type"),
            budget=data.get("budget"),
            throughput=data.get("throughput"),
            environment=data.get("environment") or {},
            capabilities=tuple(data.get("capabilities") or []),
            hard_constraints=parse_constraints("hard_constraints", ConstraintKind.HARD),
            soft_constraints=parse_constraints("soft_constraints", ConstraintKind.SOFT),
            unknowns=tuple(data.get("unknowns") or []),
            status=status,
            version=version,
        )

    def confirm(self, edits: dict[str, Any] | None = None) -> RequirementSpec:
        """安全的状态转换：draft -> confirmed

        状态转换规则：
        1. 只能从draft转到confirmed
        2. 确认时版本号+1
        3. 可以同时应用编辑
        """
        if self.status == RequirementStatus.CONFIRMED:
            raise ValueError("requirement is already confirmed")

        if edits:
            # 合并编辑
            merged = self.to_dict()
            merged.update(edits)
            spec = self.from_dict(merged)
        else:
            spec = self

        return replace(
            spec,
            status=RequirementStatus.CONFIRMED,
            version=spec.version + 1
        )

    def revise(self, edits: dict[str, Any]) -> RequirementSpec:
        """修订已确认的需求，版本号+1"""
        if not edits:
            raise ValueError("edits cannot be empty")

        merged = self.to_dict()
        merged.update(edits)
        revised = self.from_dict(merged)

        return replace(
            revised,
            status=RequirementStatus.DRAFT,  # 修订后回到草稿状态
            version=self.version + 1
        )


__all__ = [
    "ConstraintKind",
    "RequirementStatus",
    "Constraint",
    "RequirementSpec",
]
