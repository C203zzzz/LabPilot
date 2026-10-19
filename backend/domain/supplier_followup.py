"""M18 final-decision and supplier follow-up contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from backend.domain.errors import ModuleValidationError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FinalDecisionStatus(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    PENDING_REVIEW = "pending_review"


class InquiryStatus(str, Enum):
    OPEN = "open"
    SENT = "sent"
    ANSWERED = "answered"
    CANCELLED = "cancelled"


class ReplyReviewStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class FinalDecision:
    decision_id: str
    task_id: str
    status: FinalDecisionStatus = FinalDecisionStatus.PENDING_REVIEW
    explicit_user_confirmation: bool = False
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.task_id.strip():
            raise ModuleValidationError("decision_id and task_id are required", field="task_id")
        object.__setattr__(self, "status", FinalDecisionStatus(self.status))
        if self.status is not FinalDecisionStatus.PENDING_REVIEW:
            if not self.explicit_user_confirmation:
                raise ModuleValidationError(
                    "confirmed or rejected final decisions require explicit user confirmation",
                    code="explicit_user_confirmation_required",
                    field="explicit_user_confirmation",
                )
            if not (self.decided_by or "").strip():
                raise ModuleValidationError("decided_by is required", field="decided_by")
            if self.decided_at is None:
                object.__setattr__(self, "decided_at", utc_now())


@dataclass(frozen=True)
class SupplierInquiry:
    inquiry_id: str
    task_id: str
    candidate_id: str
    supplier: str
    question: str
    reason: str
    status: InquiryStatus = InquiryStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    source_question_id: str | None = None
    fact_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    quote_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("inquiry_id", "task_id", "candidate_id", "supplier", "question", "reason"):
            if not str(getattr(self, name)).strip():
                raise ModuleValidationError(f"{name} is required", field=name)
        object.__setattr__(self, "status", InquiryStatus(self.status))


@dataclass(frozen=True)
class SupplierReplyAttachment:
    name: str
    attachment_id: str | None = None
    media_type: str | None = None
    document_id: str | None = None
    url: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ModuleValidationError("attachment name is required", field="attachments")
        if not any((self.attachment_id, self.document_id, self.url)):
            raise ModuleValidationError(
                "attachment requires attachment_id, document_id, or url",
                code="missing_source_locator",
                field="attachments",
            )


@dataclass(frozen=True)
class SupplierReply:
    reply_id: str
    inquiry_id: str
    task_id: str
    supplier: str
    received_at: datetime
    original_text: str
    source_location: str
    attachments: tuple[SupplierReplyAttachment, ...] = ()
    review_status: ReplyReviewStatus = ReplyReviewStatus.PENDING_REVIEW
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None

    def __post_init__(self) -> None:
        for name in ("reply_id", "inquiry_id", "task_id", "supplier", "original_text", "source_location"):
            if not str(getattr(self, name)).strip():
                raise ModuleValidationError(f"{name} is required", field=name)
        if self.received_at.tzinfo is None:
            raise ModuleValidationError("received_at must include timezone", field="received_at")
        object.__setattr__(self, "review_status", ReplyReviewStatus(self.review_status))


def contract_to_dict(value: Any) -> dict[str, Any]:
    from dataclasses import asdict

    def convert(item: Any) -> Any:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, dict):
            return {key: convert(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(val) for val in item]
        return item

    return convert(asdict(value))


__all__ = [
    "FinalDecision", "FinalDecisionStatus", "InquiryStatus", "ReplyReviewStatus",
    "SupplierInquiry", "SupplierReply", "SupplierReplyAttachment", "contract_to_dict", "utc_now",
]
