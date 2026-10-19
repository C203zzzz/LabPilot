"""M06 product-fact and evidence contracts."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

class FactValueType(str, Enum):
    TEXT="text"; NUMBER="number"; BOOLEAN="boolean"; RANGE="range"; PRICE="price"; DATE="date"; JSON="json"
class FactStatus(str, Enum):
    EXTRACTED="extracted"; PENDING_REVIEW="pending_review"; CONFIRMED="confirmed"; REJECTED="rejected"; CONFLICTED="conflicted"; UNKNOWN="unknown"
class FactEvidenceLevel(str, Enum):
    PRIMARY="primary"; SECONDARY="secondary"; USER_PROVIDED="user_provided"; HISTORICAL="historical_tender"; UNKNOWN="unknown"

@dataclass(frozen=True)
class FactEvidence:
    evidence_id: str
    candidate_id: str
    source_type: str
    authority_score: int
    evidence_level: FactEvidenceLevel
    extracted_at: datetime
    document_id: str|None = None
    page: int|None = None
    location: str|None = None
    url: str|None = None
    body_location: str|None = None
    quote: str|None = None
    price_evidence_level: str|None = None
    tender_title: str|None = None
    publication_date: str|None = None
    currency: str|None = None
    tax_condition: str|None = None
    price_amount: float|None = None
    price_unit: str|None = None
    price_quote: str|None = None

    def __post_init__(self):
        if isinstance(self.source_type, Enum): object.__setattr__(self, "source_type", self.source_type.value)
        if not self.evidence_id.strip(): raise ValueError("evidence_id is required")
        if not self.candidate_id.strip(): raise ValueError("candidate_id is required")
        if not self.source_type.strip(): raise ValueError("source_type is required")
        if self.authority_score < 0: raise ValueError("authority_score must be non-negative")
        if not (self.document_id and (self.location or self.page is not None) or self.url and (self.body_location or self.location)):
            raise ValueError("evidence requires document location or URL body location")
        if self.source_type in {"search_snippet", "snippet"}: raise ValueError("search snippets cannot be fact evidence")
        if isinstance(self.evidence_level, str): object.__setattr__(self, "evidence_level", FactEvidenceLevel(self.evidence_level))
        if self.price_evidence_level and isinstance(self.price_evidence_level, Enum): object.__setattr__(self, "price_evidence_level", self.price_evidence_level.value)
        if self.price_evidence_level == "historical_tender" and not self.tender_title:
            raise ValueError("historical tender evidence requires tender_title")

@dataclass
class ProductFact:
    fact_id: str
    candidate_id: str
    field_name: str
    value: Any
    value_type: FactValueType
    unit: str|None = None
    evidence: list[FactEvidence] = field(default_factory=list)
    status: FactStatus = FactStatus.EXTRACTED
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: datetime|None = None
    reviewed_by: str|None = None
    conflict_ids: list[str] = field(default_factory=list)
    note: str|None = None

    def __post_init__(self):
        if not self.fact_id.strip(): raise ValueError("fact_id is required")
        if not self.candidate_id.strip(): raise ValueError("candidate_id is required")
        if not self.field_name.strip(): raise ValueError("field_name is required")
        if self.value is None or (isinstance(self.value,str) and not self.value.strip()): raise ValueError("value cannot be empty")
        if not isinstance(self.value_type, FactValueType): self.value_type = FactValueType(self.value_type)
        if not self.evidence: raise ValueError("at least one evidence is required")
        if any(e.candidate_id != self.candidate_id for e in self.evidence): raise ValueError("evidence candidate_id mismatch")

@dataclass(frozen=True)
class FactReview:
    fact_id: str
    action: str
    reviewer_id: str
    reviewed_at: datetime
    note: str|None = None
    conflict_ids: tuple[str,...] = ()

    def __post_init__(self):
        if self.action not in {"confirm", "reject", "resolve_conflict"}: raise ValueError("invalid review action")
        if not self.fact_id.strip() or not self.reviewer_id.strip(): raise ValueError("fact_id and reviewer_id are required")

__all__ = ["FactEvidence","ProductFact","FactReview","FactStatus","FactValueType","FactEvidenceLevel"]
