"""M06 product-fact and evidence contracts (immutable, validated state machine)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import Any


class FactValueType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    RANGE = "range"
    PRICE = "price"
    DATE = "date"
    JSON = "json"
    STRUCTURED = "structured"


class FactStatus(StrEnum):
    EXTRACTED = "extracted"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"


class FactEvidenceLevel(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    USER_PROVIDED = "user_provided"
    HISTORICAL = "historical_tender"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FactEvidence:
    evidence_id: str
    candidate_id: str
    source_type: str
    authority_score: int
    evidence_level: FactEvidenceLevel
    extracted_at: datetime
    document_id: str | None = None
    page: int | None = None
    location: str | None = None
    url: str | None = None
    body_location: str | None = None
    quote: str | None = None
    price_evidence_level: str | None = None
    tender_title: str | None = None
    publication_date: str | None = None
    currency: str | None = None
    tax_condition: str | None = None
    price_amount: float | None = None
    price_unit: str | None = None
    price_quote: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.source_type, Enum):
            object.__setattr__(self, "source_type", self.source_type.value)
        if not isinstance(self.evidence_id, str) or not self.evidence_id.strip():
            raise ValueError("evidence_id cannot be empty")
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if not isinstance(self.source_type, str) or not self.source_type.strip():
            raise ValueError("source_type is required")
        if isinstance(self.authority_score, bool) or not isinstance(self.authority_score, int) or not 0 <= self.authority_score <= 100:
            raise ValueError(f"authority_score must be 0-100, got {self.authority_score}")
        if self.source_type in {"search_snippet", "snippet"}:
            raise ValueError("search snippets cannot be fact evidence")
        if isinstance(self.evidence_level, str) and not isinstance(self.evidence_level, FactEvidenceLevel):
            level = "historical_tender" if self.evidence_level == "historical" else self.evidence_level
            object.__setattr__(self, "evidence_level", FactEvidenceLevel(level))
        if not isinstance(self.evidence_level, FactEvidenceLevel):
            raise ValueError(f"evidence_level must be FactEvidenceLevel, got {type(self.evidence_level)}")
        if self.price_evidence_level and isinstance(self.price_evidence_level, Enum):
            object.__setattr__(self, "price_evidence_level", self.price_evidence_level.value)
        if self.price_evidence_level == "historical_tender" and not self.tender_title:
            raise ValueError("historical tender evidence requires tender_title")
        if self.price_amount is not None:
            if isinstance(self.price_amount, bool) or not isinstance(self.price_amount, (int, float)) or self.price_amount < 0:
                raise ValueError(f"price_amount must be non-negative, got {self.price_amount}")


@dataclass(frozen=True)
class ProductFact:
    """Immutable product fact with validated review state transitions."""
    fact_id: str
    candidate_id: str
    field_name: str
    value: Any
    value_type: FactValueType
    unit: str | None = None
    evidence: tuple[FactEvidence, ...] = ()
    status: FactStatus = FactStatus.EXTRACTED
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    conflict_ids: tuple[str, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not self.fact_id.strip():
            raise ValueError("fact_id cannot be empty")
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if not isinstance(self.field_name, str) or not self.field_name.strip():
            raise ValueError("field_name cannot be empty")
        if self.value is None or (isinstance(self.value, str) and not self.value.strip()):
            raise ValueError("value cannot be empty")
        if isinstance(self.value_type, str) and not isinstance(self.value_type, FactValueType):
            object.__setattr__(self, "value_type", FactValueType(self.value_type))
        if not isinstance(self.value_type, FactValueType):
            raise ValueError(f"value_type must be FactValueType, got {type(self.value_type)}")
        if isinstance(self.status, str) and not isinstance(self.status, FactStatus):
            object.__setattr__(self, "status", FactStatus(self.status))
        if not isinstance(self.status, FactStatus):
            raise ValueError(f"status must be FactStatus, got {type(self.status)}")
        if isinstance(self.evidence, list):
            object.__setattr__(self, "evidence", tuple(self.evidence))
        if isinstance(self.conflict_ids, list):
            object.__setattr__(self, "conflict_ids", tuple(self.conflict_ids))
        if not self.evidence:
            raise ValueError("fact must have at least one evidence")
        for item in self.evidence:
            if item.candidate_id != self.candidate_id:
                raise ValueError(f"evidence candidate_id mismatch: {item.candidate_id} != {self.candidate_id}")

    def confirm(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        if self.status == FactStatus.REJECTED:
            raise ValueError("cannot confirm rejected fact, must create new fact")
        if self.status == FactStatus.CONFLICTED:
            raise ValueError("cannot confirm conflicted fact, must resolve conflict first")
        if self.status == FactStatus.CONFIRMED:
            raise ValueError("fact is already confirmed")
        return replace(
            self,
            status=FactStatus.CONFIRMED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note,
        )

    def reject(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        if self.status == FactStatus.CONFIRMED:
            raise ValueError("cannot reject confirmed fact, must create new revision")
        if self.status == FactStatus.CONFLICTED:
            raise ValueError("cannot reject conflicted fact, must resolve conflict first")
        if self.status == FactStatus.REJECTED:
            raise ValueError("fact is already rejected")
        return replace(
            self,
            status=FactStatus.REJECTED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note,
        )

    def mark_conflicted(self, conflict_ids: tuple[str, ...] | list[str]) -> ProductFact:
        if not conflict_ids:
            raise ValueError("conflict_ids cannot be empty")
        ids = tuple(sorted(set(conflict_ids)))
        if self.fact_id in ids:
            raise ValueError("conflict_ids cannot contain self")
        return replace(self, status=FactStatus.CONFLICTED, conflict_ids=ids)

    def resolve_conflict(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        if self.status != FactStatus.CONFLICTED:
            raise ValueError("fact is not conflicted")
        return replace(
            self,
            status=FactStatus.CONFIRMED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note,
            conflict_ids=(),
        )


def reconcile_facts(facts: list[ProductFact]) -> list[ProductFact]:
    """Group facts by candidate/field and mark explicit value disagreements as conflicted."""
    if not isinstance(facts, list):
        raise ValueError("facts must be a list")
    groups: dict[tuple[str, str], list[ProductFact]] = {}
    for fact in facts:
        if not isinstance(fact, ProductFact):
            raise ValueError(f"expected ProductFact, got {type(fact)}")
        groups.setdefault((fact.candidate_id, fact.field_name), []).append(fact)

    result: list[ProductFact] = []
    for members in groups.values():
        distinct = {repr((m.value, m.unit, m.value_type.value)) for m in members}
        if len(distinct) > 1:
            fact_ids = tuple(m.fact_id for m in members)
            for fact in members:
                conflict_ids = tuple(fid for fid in fact_ids if fid != fact.fact_id)
                result.append(fact.mark_conflicted(conflict_ids))
        else:
            result.extend(members)
    return result


@dataclass(frozen=True)
class FactReview:
    fact_id: str
    action: str
    reviewer_id: str
    reviewed_at: datetime
    note: str | None = None
    conflict_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in {"confirm", "reject", "resolve_conflict"}:
            raise ValueError("invalid review action")
        if not self.fact_id.strip() or not self.reviewer_id.strip():
            raise ValueError("fact_id and reviewer_id are required")


__all__ = [
    "FactEvidence",
    "ProductFact",
    "FactReview",
    "FactStatus",
    "FactValueType",
    "FactEvidenceLevel",
    "reconcile_facts",
]
