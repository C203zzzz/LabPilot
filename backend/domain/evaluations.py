"""M07 requirement constraint evaluation contracts."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

class ConstraintEvaluationStatus(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

class EvaluationStatus(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    NEEDS_REVIEW = "needs_review"

@dataclass(frozen=True)
class ConstraintEvaluation:
    constraint_name: str
    kind: str
    status: ConstraintEvaluationStatus
    reason: str
    fact_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    missing_reason: str | None = None
    actual_value: Any = None
    expected_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"constraint_name": self.constraint_name, "kind": self.kind,
                "status": self.status.value, "reason": self.reason,
                "fact_ids": list(self.fact_ids), "evidence_ids": list(self.evidence_ids),
                "missing_reason": self.missing_reason, "actual_value": self.actual_value,
                "expected_value": self.expected_value}

@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    status: EvaluationStatus
    eligible: bool
    hard_constraints: list[ConstraintEvaluation] = field(default_factory=list)
    soft_constraints: list[ConstraintEvaluation] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "status": self.status.value,
                "eligible": self.eligible,
                "hard_constraints": [x.to_dict() for x in self.hard_constraints],
                "soft_constraints": [x.to_dict() for x in self.soft_constraints],
                "reason": self.reason}

__all__ = ["ConstraintEvaluationStatus", "EvaluationStatus", "ConstraintEvaluation", "CandidateEvaluation"]
