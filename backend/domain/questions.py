"""M10 supplier-question contracts."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum

from backend.domain.errors import ModuleValidationError

class QuestionPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class QuestionCategory(StrEnum):
    HARD_CONSTRAINT = "hard_constraint"
    FACT = "fact"
    QUOTE = "quote"
    PRICE_COMPARABILITY = "price_comparability"
    SERVICE = "service"
    REVIEW = "review"

class CandidateDisposition(StrEnum):
    PREFERRED = "preferred"
    ALTERNATIVE = "alternative"
    NEEDS_REVIEW = "needs_review"
    INELIGIBLE = "ineligible"
    UNCLASSIFIED = "unclassified"

@dataclass(frozen=True)
class QuestionEvidence:
    candidate_id: str
    field: str | None = None
    fact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    quote_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ModuleValidationError("candidate_id is required", code="missing_association_id", field="candidate_id")
        if self.field is not None and (not isinstance(self.field, str) or not self.field.strip()):
            raise ModuleValidationError("field must be non-empty", code="invalid_input", field="field")
        for name in ("fact_ids", "evidence_ids", "quote_ids"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ModuleValidationError(f"{name} must contain non-empty ids", code="missing_association_id", field=name)

    def to_dict(self):
        return {"candidate_id": self.candidate_id, "field": self.field,
                "fact_ids": list(self.fact_ids), "evidence_ids": list(self.evidence_ids),
                "quote_ids": list(self.quote_ids)}

@dataclass(frozen=True)
class SupplierQuestion:
    question_id: str
    candidate_id: str
    question: str
    reason: str
    category: QuestionCategory | str
    priority: QuestionPriority | str
    requested_answer_format: str
    evidence: QuestionEvidence
    candidate_disposition: CandidateDisposition | str = CandidateDisposition.UNCLASSIFIED

    def __post_init__(self):
        for name in ("question_id", "candidate_id", "question", "reason", "requested_answer_format"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ModuleValidationError(f"{name} is required", code="invalid_input", field=name)
        if self.evidence.candidate_id != self.candidate_id:
            raise ModuleValidationError("evidence candidate_id mismatch", code="association_mismatch", field="evidence")
        try:
            object.__setattr__(self, "category", QuestionCategory(self.category))
            object.__setattr__(self, "priority", QuestionPriority(self.priority))
            object.__setattr__(self, "candidate_disposition", CandidateDisposition(self.candidate_disposition))
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("invalid question enum", code="invalid_input") from exc

    @property
    def field(self): return self.evidence.field
    def to_dict(self):
        return {"question_id": self.question_id, "candidate_id": self.candidate_id,
                "question": self.question, "reason": self.reason,
                "category": self.category.value, "priority": self.priority.value,
                "candidate_disposition": self.candidate_disposition.value,
                "requested_answer_format": self.requested_answer_format,
                "evidence": self.evidence.to_dict(),
                "field": self.evidence.field, "fact_ids": list(self.evidence.fact_ids),
                "evidence_ids": list(self.evidence.evidence_ids), "quote_ids": list(self.evidence.quote_ids)}

@dataclass(frozen=True)
class QuestionList:
    questions: tuple[SupplierQuestion, ...] = ()
    rule_version: str = "m10-v1"
    @property
    def by_candidate(self):
        groups: dict[str, list[SupplierQuestion]] = {}
        for question in self.questions:
            groups.setdefault(question.candidate_id, []).append(question)
        return groups

    def to_dict(self):
        return {"questions": [q.to_dict() for q in self.questions],
                "candidate_groups": {key: [q.to_dict() for q in value]
                                     for key, value in self.by_candidate.items()},
                "rule_version": self.rule_version}
    def __iter__(self): return iter(self.questions)

__all__ = ["QuestionPriority", "QuestionCategory", "CandidateDisposition", "QuestionEvidence", "SupplierQuestion", "QuestionList"]
