"""M18 workflow rules; persistence is owned by the API repository boundary."""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from backend.domain.errors import ModuleValidationError
from backend.domain.facts import FactEvidence, FactEvidenceLevel, FactStatus, ProductFact
from backend.domain.supplier_followup import (
    FinalDecision, FinalDecisionStatus, InquiryStatus, ReplyReviewStatus,
    SupplierInquiry, SupplierReply, SupplierReplyAttachment, utc_now,
)
from backend.services.fact_service import confirm_fact, make_fact, reject_fact, review_fact_immutable


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def make_pending_decision(task_id: str) -> FinalDecision:
    return FinalDecision(f"decision-{task_id}", task_id)


def decide_task(task_id: str, status: str, *, explicit_user_confirmation: bool,
                decided_by: str | None = None, note: str | None = None) -> FinalDecision:
    selected = FinalDecisionStatus(status)
    return FinalDecision(
        f"decision-{task_id}", task_id, selected, explicit_user_confirmation,
        decided_by, utc_now() if selected is not FinalDecisionStatus.PENDING_REVIEW else None, note,
    )


def create_inquiry(task_id: str, payload: dict[str, Any]) -> SupplierInquiry:
    return SupplierInquiry(
        inquiry_id=_id("inquiry"), task_id=task_id,
        candidate_id=str(payload.get("candidate_id", "")), supplier=str(payload.get("supplier", "")),
        question=str(payload.get("question", "")), reason=str(payload.get("reason", "")),
        status=InquiryStatus(payload.get("status", InquiryStatus.OPEN.value)),
        source_question_id=payload.get("source_question_id"),
        fact_ids=tuple(payload.get("fact_ids") or ()), evidence_ids=tuple(payload.get("evidence_ids") or ()),
        quote_ids=tuple(payload.get("quote_ids") or ()),
    )


def create_reply(task_id: str, inquiry: dict[str, Any], payload: dict[str, Any]) -> SupplierReply:
    supplier = str(payload.get("supplier", "")).strip()
    if supplier.casefold() != str(inquiry.get("supplier", "")).strip().casefold():
        raise ModuleValidationError(
            "reply supplier does not match inquiry supplier",
            code="supplier_mismatch", field="supplier",
        )
    received_at = payload.get("received_at")
    if isinstance(received_at, str):
        try:
            received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ModuleValidationError("received_at must be ISO 8601", field="received_at") from exc
    attachments = tuple(SupplierReplyAttachment(**item) for item in payload.get("attachments") or ())
    return SupplierReply(
        reply_id=_id("reply"), inquiry_id=str(inquiry["inquiry_id"]), task_id=task_id,
        supplier=supplier, received_at=received_at, original_text=str(payload.get("original_text", "")),
        source_location=str(payload.get("source_location", "")), attachments=attachments,
    )


def review_reply(reply: SupplierReply, status: str, *, explicit_user_confirmation: bool,
                 reviewed_by: str, note: str | None = None) -> SupplierReply:
    selected = ReplyReviewStatus(status)
    if selected is not ReplyReviewStatus.PENDING_REVIEW and not explicit_user_confirmation:
        raise ModuleValidationError(
            "reply confirmation or rejection requires explicit user confirmation",
            code="explicit_user_confirmation_required", field="explicit_user_confirmation",
        )
    if selected is not ReplyReviewStatus.PENDING_REVIEW and not reviewed_by.strip():
        raise ModuleValidationError("reviewed_by is required", field="reviewed_by")
    out = deepcopy(reply)
    object.__setattr__(out, "review_status", selected)
    object.__setattr__(out, "reviewed_by", reviewed_by or None)
    object.__setattr__(out, "reviewed_at", utc_now() if selected is not ReplyReviewStatus.PENDING_REVIEW else None)
    object.__setattr__(out, "review_note", note)
    return out


def reply_to_fact_draft(reply: SupplierReply, *, candidate_id: str, field_name: str, value: Any,
                        value_type: str, unit: str | None = None) -> ProductFact:
    if reply.review_status is not ReplyReviewStatus.CONFIRMED:
        raise ModuleValidationError(
            "only a confirmed supplier reply can create a fact draft",
            code="reply_not_confirmed", field="review_status",
        )
    evidence = FactEvidence(
        evidence_id=_id("evidence"), candidate_id=candidate_id, source_type="supplier_reply",
        authority_score=50, evidence_level=FactEvidenceLevel.USER_PROVIDED, extracted_at=utc_now(),
        document_id=f"supplier-reply:{reply.reply_id}", location=reply.source_location,
        quote=reply.original_text,
    )
    return make_fact(
        candidate_id, field_name, value, value_type, evidence,
        fact_id=_id("fact"), unit=unit, status=FactStatus.PENDING_REVIEW,
    )


def reply_from_dict(raw: dict[str, Any]) -> SupplierReply:
    return SupplierReply(
        reply_id=raw["reply_id"], inquiry_id=raw["inquiry_id"], task_id=raw["task_id"],
        supplier=raw["supplier"], received_at=datetime.fromisoformat(raw["received_at"]),
        original_text=raw["original_text"], source_location=raw["source_location"],
        attachments=tuple(SupplierReplyAttachment(**item) for item in raw.get("attachments", [])),
        review_status=raw.get("review_status", "pending_review"), reviewed_by=raw.get("reviewed_by"),
        reviewed_at=datetime.fromisoformat(raw["reviewed_at"]) if raw.get("reviewed_at") else None,
        review_note=raw.get("review_note"),
    )


def fact_from_dict(raw: dict[str, Any]) -> ProductFact:
    evidence = [FactEvidence(
        evidence_id=item["evidence_id"], candidate_id=item["candidate_id"],
        source_type=item["source_type"], authority_score=item["authority_score"],
        evidence_level=item["evidence_level"], extracted_at=datetime.fromisoformat(item["extracted_at"]),
        document_id=item.get("document_id"), page=item.get("page"), location=item.get("location"),
        url=item.get("url"), body_location=item.get("body_location"), quote=item.get("quote"),
        price_evidence_level=item.get("price_evidence_level"), tender_title=item.get("tender_title"),
        publication_date=item.get("publication_date"), currency=item.get("currency"),
        tax_condition=item.get("tax_condition"),
    ) for item in raw.get("evidence", [])]
    return ProductFact(
        fact_id=raw["fact_id"], candidate_id=raw["candidate_id"], field_name=raw["field_name"],
        value=raw["value"], value_type=raw["value_type"], unit=raw.get("unit"), evidence=evidence,
        status=raw.get("status", "pending_review"), extracted_at=datetime.fromisoformat(raw["extracted_at"]),
        reviewed_at=datetime.fromisoformat(raw["reviewed_at"]) if raw.get("reviewed_at") else None,
        reviewed_by=raw.get("reviewed_by"), conflict_ids=list(raw.get("conflict_ids") or []), note=raw.get("note"),
    )


def review_fact(raw: dict[str, Any], action: str, reviewer_id: str, note: str | None = None) -> ProductFact:
    fact = fact_from_dict(raw)
    if action == "confirm":
        return review_fact_immutable(fact, action, reviewer_id, note)
    if action == "reject":
        return review_fact_immutable(fact, action, reviewer_id, note)
    raise ModuleValidationError("action must be confirm or reject", field="action")


__all__ = [
    "create_inquiry", "create_reply", "decide_task", "fact_from_dict", "make_pending_decision",
    "reply_from_dict", "reply_to_fact_draft", "review_fact", "review_reply",
]
