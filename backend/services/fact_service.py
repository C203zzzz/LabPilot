"""Pure, persistence-free M06 fact creation, adaptation and review."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1

from backend.domain.facts import (
    FactEvidence,
    FactEvidenceLevel,
    FactReview,
    FactStatus,
    FactValueType,
    ProductFact,
    reconcile_facts,
)
from backend.domain.rag import EvidenceStatus
from backend.domain.search import BodyStatus, SourceType


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(*parts) -> str:
    return sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def evidence_from_retrieved(retrieved, candidate_id: str, *, extracted_at=None) -> FactEvidence:
    c = retrieved.chunk
    if getattr(c, "evidence_status", EvidenceStatus.AVAILABLE) not in (EvidenceStatus.AVAILABLE, "available") or not (c.text or "").strip():
        raise ValueError("retrieved evidence is unavailable or empty")
    if not candidate_id.strip():
        raise ValueError("candidate_id is required")
    st = getattr(c.source_type, "value", c.source_type)
    if st in ("search_snippet", "snippet"):
        raise ValueError("search snippets cannot be fact evidence")
    level = (
        FactEvidenceLevel.USER_PROVIDED
        if st == "user_document"
        else (FactEvidenceLevel.PRIMARY if st == "official_source" else FactEvidenceLevel.SECONDARY)
    )
    return FactEvidence(
        _id(candidate_id, c.chunk_id),
        candidate_id,
        st or "unknown",
        max(0, min(100, int(c.authority_score or 0))),
        level,
        extracted_at or _now(),
        document_id=c.document_id,
        page=c.page,
        location=c.location,
        url=c.source_url,
        body_location=c.location,
        quote=c.text,
    )


def evidence_from_source(source, candidate_id: str, *, extracted_at=None) -> FactEvidence:
    status = getattr(source, "body_status", BodyStatus.NOT_REQUESTED)
    if status not in (BodyStatus.FETCHED, "fetched") or not (getattr(source, "body_text", None) or "").strip():
        raise ValueError("source body must be fetched and non-empty")
    st = getattr(source.source_type, "value", source.source_type)
    if st == "search_snippet":
        raise ValueError("search snippets cannot be fact evidence")
    level = (
        FactEvidenceLevel.HISTORICAL
        if getattr(getattr(source, "price_evidence_level", None), "value", None) == "historical_tender"
        else (FactEvidenceLevel.PRIMARY if st.startswith("manufacturer") else FactEvidenceLevel.SECONDARY)
    )
    return FactEvidence(
        _id(candidate_id, source.url),
        candidate_id,
        st,
        max(0, min(100, int(getattr(source, "authority_score", 0)))),
        level,
        extracted_at or _now(),
        url=source.url,
        body_location=getattr(source, "source_location", None) or "body",
        quote=source.body_text,
        price_evidence_level=getattr(getattr(source, "price_evidence_level", None), "value", None),
        tender_title=getattr(source, "tender_title", None),
        publication_date=getattr(source, "publication_date", None),
        currency=getattr(source, "currency", None),
        tax_condition=getattr(source, "tax_condition", None),
        price_amount=getattr(source, "price_amount", None),
        price_unit=getattr(source, "price_unit", None),
        price_quote=getattr(source, "price_quote", None),
    )


def make_fact(candidate_id, field_name, value, value_type, evidence, *, fact_id=None, unit=None, status=FactStatus.EXTRACTED) -> ProductFact:
    return ProductFact(
        fact_id or _id(candidate_id, field_name, value),
        candidate_id,
        field_name,
        value,
        FactValueType(value_type),
        unit,
        (evidence,) if isinstance(evidence, FactEvidence) else tuple(evidence),
        FactStatus(status),
    )


def confirm_fact(fact: ProductFact, reviewer_id: str, note=None) -> ProductFact:
    # The pure helper stays idempotent for document adapters; the HTTP review
    # route calls review_fact_immutable directly so duplicate confirmation is
    # still rejected by the state machine.
    if fact.status is FactStatus.CONFIRMED:
        if not reviewer_id.strip():
            raise ValueError("reviewer_id is required")
        return fact
    return review_fact_immutable(fact, "confirm", reviewer_id, note)


def reject_fact(fact: ProductFact, reviewer_id: str, note=None) -> ProductFact:
    return review_fact_immutable(fact, "reject", reviewer_id, note)


def resolve_conflict(fact: ProductFact, reviewer_id: str, *, chosen_fact_id=None, note=None) -> ProductFact:
    return fact.resolve_conflict(reviewer_id, note=note)


def mark_conflicted(fact: ProductFact, conflict_ids) -> ProductFact:
    return fact.mark_conflicted(conflict_ids)


def review_fact_immutable(fact: ProductFact, action: str, reviewer_id: str, note=None) -> ProductFact:
    """Apply the immutable fact review state machine directly."""
    if action not in {"confirm", "reject"}:
        raise ValueError("action must be confirm or reject")
    return fact.confirm(reviewer_id, note) if action == "confirm" else fact.reject(reviewer_id, note)


class FactService:
    """Stateless facade; callers retain returned immutable-review snapshots."""
    evidence_from_retrieved = staticmethod(evidence_from_retrieved)
    evidence_from_source = staticmethod(evidence_from_source)
    make_fact = staticmethod(make_fact)
    confirm_fact = staticmethod(confirm_fact)
    reject_fact = staticmethod(reject_fact)
    resolve_conflict = staticmethod(resolve_conflict)
    reconcile_facts = staticmethod(reconcile_facts)


__all__ = [
    "FactService",
    "evidence_from_retrieved",
    "evidence_from_source",
    "make_fact",
    "confirm_fact",
    "reject_fact",
    "resolve_conflict",
    "mark_conflicted",
    "reconcile_facts",
    "review_fact_immutable",
    "FactReview",
    "SourceType",
]
