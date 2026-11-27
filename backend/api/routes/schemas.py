"""Pydantic request schemas shared across workflow API route modules."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    scenario: str = Field(min_length=1)


class TaskUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class RequirementConfirm(BaseModel):
    requirement: dict[str, Any]
    edits: dict[str, Any] | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)


class ReportBatchRequest(BaseModel):
    report_ids: list[str] = Field(min_length=1, max_length=200)


class TaskBatchRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=200)


class ReportCreate(BaseModel):
    requirement: dict[str, Any]
    candidates: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    ranking: dict[str, Any] | None = None
    quotes: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    questions: dict[str, Any] | None = None
    format: str = "markdown"
    revision_reason: str | None = None
    expected_parent_version_id: str | None = None


class CandidateDecision(BaseModel):
    status: str = Field(pattern="^(confirmed|excluded|pending_confirmation)$")
    note: str | None = None


class CandidateBatchDecision(BaseModel):
    candidate_id: str
    status: str = Field(pattern="^(confirmed|excluded|pending_confirmation)$")
    note: str | None = None


class SearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=500)
    plan_id: str | None = None
    candidate_ids: list[str] = []
    limit: int = Field(default=10, ge=1, le=50)
    purpose: str = "official"
    fetch_body: bool = True


class SearchPlanRequest(BaseModel):
    purpose: str = "official"
    limit: int = Field(default=10, ge=1, le=50)


class WebCandidateDiscoveryRequest(BaseModel):
    source_urls: list[str] = Field(default_factory=list)
    model: str | None = None


class SourceDecision(BaseModel):
    url: str
    status: str = Field(pattern="^(confirmed|rejected|pending_review)$")
    note: str | None = None


class EvaluationRequest(BaseModel):
    requirement: dict[str, Any]
    candidates: list[dict[str, Any]]
    facts: list[dict[str, Any]] = []


class RankingRequest(BaseModel):
    evaluations: list[dict[str, Any]]
    facts: list[dict[str, Any]] = []
    weights: dict[str, float] | None = None


class ComparisonRequest(BaseModel):
    quotes: list[dict[str, Any]]
    candidate_id: str | None = None


class QuestionsRequest(BaseModel):
    evaluations: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    quotes: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    recommendations: Any = []


class DeepSeekAnalysisRequest(BaseModel):
    scenario: str | None = None
    model: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=120)


class ProviderHealthRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    modes: list[str] = Field(default_factory=list)
    credentials: dict[str, str] = Field(default_factory=dict)


class AnalysisDecision(BaseModel):
    analysis_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    status: str = Field(pattern="^(confirmed|rejected|pending_review)$")
    field_statuses: dict[str, str] = Field(default_factory=dict)
    field_values: dict[str, Any] = Field(default_factory=dict)
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = None


class ArchiveRequest(BaseModel):
    note: str | None = None


class RestoreRequest(BaseModel):
    archive_id: str | None = None


class RAGQueryRequest(BaseModel):
    text: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_model: str | None = None
    parameter_terms: list[str] = []
    requirement_terms: list[str] = []


class FinalDecisionRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|rejected|pending_review)$")
    explicit_user_confirmation: bool = False
    decided_by: str | None = None
    note: str | None = None


class SupplierInquiryItem(BaseModel):
    candidate_id: str = Field(min_length=1)
    supplier: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: str = Field(default="open", pattern="^(open|sent|answered|cancelled)$")
    source_question_id: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    quote_ids: list[str] = Field(default_factory=list)


class SupplierInquiriesCreate(BaseModel):
    items: list[SupplierInquiryItem] = Field(min_length=1)


class SupplierReplyAttachmentRequest(BaseModel):
    name: str = Field(min_length=1)
    attachment_id: str | None = None
    media_type: str | None = None
    document_id: str | None = None
    url: str | None = None
    location: str | None = None


class SupplierReplyCreate(BaseModel):
    supplier: str = Field(min_length=1)
    received_at: datetime
    original_text: str = Field(min_length=1)
    source_location: str = Field(min_length=1)
    attachments: list[SupplierReplyAttachmentRequest] = Field(default_factory=list)


class SupplierReplyReview(BaseModel):
    status: str = Field(pattern="^(confirmed|rejected|pending_review)$")
    explicit_user_confirmation: bool = False
    reviewed_by: str = ""
    note: str | None = None


class FactDraftCreate(BaseModel):
    candidate_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    value: Any
    value_type: str = Field(pattern="^(text|number|boolean|range|price|date|json)$")
    unit: str | None = None


class WebFactDraftCreate(FactDraftCreate):
    url: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=3000)


class FactReviewRequest(BaseModel):
    action: str = Field(pattern="^(confirm|reject)$")
    reviewer_id: str = Field(min_length=1)
    note: str | None = None


class ReevaluateRequest(BaseModel):
    candidate_ids: list[str] = Field(default_factory=list)
