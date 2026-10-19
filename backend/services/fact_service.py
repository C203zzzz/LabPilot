"""Pure, persistence-free M06 fact creation, adaptation and review."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha1
from copy import deepcopy
from backend.domain.facts import FactEvidence, ProductFact, FactStatus, FactValueType, FactEvidenceLevel, FactReview
from backend.domain.rag import EvidenceStatus
from backend.domain.search import BodyStatus, SourceType
from backend.domain.facts_improved import (
    FactEvidence as ImmutableFactEvidence,
    FactEvidenceLevel as ImmutableEvidenceLevel,
    FactStatus as ImmutableFactStatus,
    FactValueType as ImmutableFactValueType,
    ProductFact as ImmutableProductFact,
)

def _now(): return datetime.now(timezone.utc)
def _id(*parts): return sha1("|".join(map(str,parts)).encode()).hexdigest()[:16]

def evidence_from_retrieved(retrieved, candidate_id: str, *, extracted_at=None) -> FactEvidence:
    c = retrieved.chunk
    if getattr(c, "evidence_status", EvidenceStatus.AVAILABLE) not in (EvidenceStatus.AVAILABLE, "available") or not (c.text or "").strip():
        raise ValueError("retrieved evidence is unavailable or empty")
    if not candidate_id.strip(): raise ValueError("candidate_id is required")
    st = getattr(c.source_type, "value", c.source_type)
    if st in ("search_snippet", "snippet"): raise ValueError("search snippets cannot be fact evidence")
    return FactEvidence(_id(candidate_id,c.chunk_id), candidate_id, st or "unknown", int(c.authority_score or 0), FactEvidenceLevel.USER_PROVIDED if st=="user_document" else (FactEvidenceLevel.PRIMARY if st=="official_source" else FactEvidenceLevel.SECONDARY), extracted_at or _now(), document_id=c.document_id, page=c.page, location=c.location, url=c.source_url, body_location=c.location, quote=c.text)

def evidence_from_source(source, candidate_id: str, *, extracted_at=None) -> FactEvidence:
    status = getattr(source, "body_status", BodyStatus.NOT_REQUESTED)
    if status not in (BodyStatus.FETCHED, "fetched") or not (getattr(source,"body_text",None) or "").strip():
        raise ValueError("source body must be fetched and non-empty")
    st = getattr(source.source_type, "value", source.source_type)
    if st == "search_snippet": raise ValueError("search snippets cannot be fact evidence")
    level = FactEvidenceLevel.HISTORICAL if getattr(getattr(source,"price_evidence_level",None),"value",None)=="historical_tender" else (FactEvidenceLevel.PRIMARY if st.startswith("manufacturer") else FactEvidenceLevel.SECONDARY)
    return FactEvidence(_id(candidate_id,source.url),candidate_id,st,int(getattr(source,"authority_score",0)),level,extracted_at or _now(),url=source.url,body_location=getattr(source,"source_location",None) or "body",quote=source.body_text,price_evidence_level=getattr(getattr(source,"price_evidence_level",None),"value",None),tender_title=getattr(source,"tender_title",None),publication_date=getattr(source,"publication_date",None),currency=getattr(source,"currency",None),tax_condition=getattr(source,"tax_condition",None),price_amount=getattr(source,"price_amount",None),price_unit=getattr(source,"price_unit",None),price_quote=getattr(source,"price_quote",None))

def make_fact(candidate_id, field_name, value, value_type, evidence, *, fact_id=None, unit=None, status=FactStatus.EXTRACTED):
    fact = ProductFact(
        fact_id or _id(candidate_id,field_name,value), candidate_id, field_name,
        value, FactValueType(value_type), unit,
        [evidence] if isinstance(evidence, FactEvidence) else list(evidence),
        FactStatus(status),
    )
    # Validate every newly-created fact with the immutable state machine while
    # retaining the legacy DTO shape required by existing persisted records.
    _immutable_fact_snapshot(fact)
    return fact


def _immutable_fact_snapshot(fact: ProductFact) -> ImmutableProductFact:
    level_map = {
        "historical_tender": ImmutableEvidenceLevel.HISTORICAL,
        "primary": ImmutableEvidenceLevel.PRIMARY,
        "secondary": ImmutableEvidenceLevel.SECONDARY,
        "user_provided": ImmutableEvidenceLevel.USER_PROVIDED,
        "unknown": ImmutableEvidenceLevel.USER_PROVIDED,
    }
    evidence = tuple(ImmutableFactEvidence(
        evidence_id=e.evidence_id, candidate_id=e.candidate_id,
        source_type=str(getattr(e.source_type, "value", e.source_type)),
        authority_score=max(0, min(100, int(e.authority_score))),
        evidence_level=level_map.get(getattr(e.evidence_level, "value", e.evidence_level), ImmutableEvidenceLevel.USER_PROVIDED),
        extracted_at=e.extracted_at, document_id=e.document_id, page=e.page,
        location=e.location, url=e.url, body_location=e.body_location,
        quote=e.quote, price_evidence_level=e.price_evidence_level,
        tender_title=e.tender_title, publication_date=e.publication_date,
        currency=e.currency, tax_condition=e.tax_condition,
        price_amount=e.price_amount, price_unit=e.price_unit,
        price_quote=e.price_quote,
    ) for e in fact.evidence)
    type_map = {
        "json": ImmutableFactValueType.STRUCTURED,
        "price": ImmutableFactValueType.STRUCTURED,
        "date": ImmutableFactValueType.TEXT,
    }
    value_type = type_map.get(getattr(fact.value_type, "value", fact.value_type), getattr(fact.value_type, "value", fact.value_type))
    status_map = {
        "pending_review": ImmutableFactStatus.EXTRACTED,
        "unknown": ImmutableFactStatus.EXTRACTED,
    }
    status = status_map.get(getattr(fact.status, "value", fact.status), getattr(fact.status, "value", fact.status))
    return ImmutableProductFact(
        fact_id=fact.fact_id, candidate_id=fact.candidate_id,
        field_name=fact.field_name, value=fact.value,
        value_type=ImmutableFactValueType(value_type), unit=fact.unit,
        evidence=evidence, status=ImmutableFactStatus(status),
        reviewed_by=fact.reviewed_by, reviewed_at=fact.reviewed_at,
        note=fact.note, conflict_ids=tuple(fact.conflict_ids or ()),
    )

def confirm_fact(fact: ProductFact, reviewer_id: str, note=None):
    # The legacy pure helper has historically been idempotent and is used by
    # document adapters.  The HTTP review route calls review_fact_immutable
    # directly, so user-facing state transitions still reject a duplicate
    # confirmation according to the hardened state machine.
    if fact.status is FactStatus.CONFIRMED:
        if not reviewer_id.strip():
            raise ValueError("reviewer_id is required")
        return deepcopy(fact)
    return review_fact_immutable(fact, "confirm", reviewer_id, note)
def reject_fact(fact, reviewer_id, note=None):
    return review_fact_immutable(fact, "reject", reviewer_id, note)
def resolve_conflict(fact, reviewer_id, *, chosen_fact_id=None, note=None):
    if fact.status != FactStatus.CONFLICTED: raise ValueError("fact is not conflicted")
    if not reviewer_id.strip(): raise ValueError("reviewer_id is required")
    out=deepcopy(fact); out.status=FactStatus.CONFIRMED; out.reviewed_by=reviewer_id; out.reviewed_at=_now(); out.note=note or out.note; return out

def mark_conflicted(fact, conflict_ids):
    out=deepcopy(fact); out.status=FactStatus.CONFLICTED; out.conflict_ids=sorted(set(conflict_ids)); return out

def reconcile_facts(facts):
    """Group facts by candidate/field; explicit value disagreements remain visible as conflicts."""
    groups={}
    for f in facts: groups.setdefault((f.candidate_id,f.field_name),[]).append(f)
    out=[]
    for members in groups.values():
        distinct={repr((m.value,m.unit,m.value_type.value)) for m in members}
        if len(distinct)>1:
            ids=[m.fact_id for m in members]
            out.extend(mark_conflicted(m, [x for x in ids if x != m.fact_id]) for m in members)
        else: out.extend(members)
    return out


def review_fact_immutable(fact: ProductFact, action: str, reviewer_id: str, note=None):
    """Use the hardened immutable fact state machine, preserving legacy DTOs."""
    if action not in {"confirm", "reject"}:
        raise ValueError("action must be confirm or reject")
    immutable = _immutable_fact_snapshot(fact)
    updated = immutable.confirm(reviewer_id, note) if action == "confirm" else immutable.reject(reviewer_id, note)
    out = deepcopy(fact)
    out.status = FactStatus.CONFIRMED if updated.status is ImmutableFactStatus.CONFIRMED else FactStatus.REJECTED
    out.reviewed_by, out.reviewed_at, out.note = updated.reviewed_by, updated.reviewed_at, updated.note
    return out

class FactService:
    """Stateless facade; callers retain returned immutable-review snapshots."""
    evidence_from_retrieved = staticmethod(evidence_from_retrieved)
    evidence_from_source = staticmethod(evidence_from_source)
    make_fact = staticmethod(make_fact)
    confirm = staticmethod(confirm_fact)
    reject = staticmethod(reject_fact)
    resolve_conflict = staticmethod(resolve_conflict)
    reconcile = staticmethod(reconcile_facts)

# Stable descriptive aliases for integration callers.
adapt_retrieved_evidence = evidence_from_retrieved
adapt_source_record = evidence_from_source
create_fact = make_fact
confirm = confirm_fact
reject = reject_fact
resolve = resolve_conflict

__all__=["evidence_from_retrieved","evidence_from_source","make_fact","confirm_fact","review_fact_immutable","reject_fact","resolve_conflict","mark_conflicted","reconcile_facts","FactService","adapt_retrieved_evidence","adapt_source_record","create_fact","confirm","reject","resolve"]
