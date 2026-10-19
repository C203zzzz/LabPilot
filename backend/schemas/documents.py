from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DocumentType = Literal["pdf", "image", "spreadsheet", "csv", "text", "webpage", "unknown"]
DocumentCategory = Literal["product", "quote", "other"]
PageExtractionRoute = Literal["native_text", "vision"]


@dataclass
class ParseWarning:
    code: str
    message: str
    location: str | None = None
    severity: Literal["warning", "error"] = "warning"


@dataclass
class DocumentPage:
    number: int
    text: str = ""
    source_location: str | None = None
    document_id: str | None = None
    # These fields are optional-by-default so records persisted before the
    # page-routing pipeline remain deserializable through ``DocumentPage``.
    route: PageExtractionRoute = "native_text"
    quality: dict[str, Any] = field(default_factory=dict)
    route_reason: str | None = None
    provenance: list[dict[str, Any]] = field(default_factory=list)
    regions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DocumentTable:
    index: int
    rows: list[list[str]] = field(default_factory=list)
    sheet: str | None = None
    source_location: str | None = None
    # Explicit geometric spans; empty cells alone never imply inheritance.
    cells: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DocumentImage:
    index: int
    source_location: str | None = None
    media_type: str | None = None
    sha256: str | None = None
    ocr_status: Literal["not_requested", "pending", "completed", "failed"] = "not_requested"
    # OCR/model output is kept beside its source location and never promoted
    # to a confirmed fact.  The text is the adapter's verbatim output so that
    # character mistakes remain auditable instead of being silently corrected.
    ocr_text: str = ""
    structured_fields: dict[str, Any] = field(default_factory=dict)
    # 视觉调用失败时的技术细节：异常类名、稳定错误码、脱敏后的原文与出错行。
    # 它只给排查用（与 ``run['failures']`` 同形，见 docs/209 §5）；给用户看的话
    # 走 ``gaps``，两处分开写。成功时保持 ``None``；旧记录没有这个键也能反序列化。
    failure: dict[str, Any] | None = None


@dataclass
class ModelExtraction:
    """Optional model output; deliberately separate from source-derived content."""

    status: Literal["not_run", "completed", "failed"] = "not_run"
    provider: str | None = None
    review_status: Literal["pending_review", "confirmed", "rejected"] = "pending_review"
    text: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)


@dataclass
class DocumentRecord:
    id: str
    source_name: str
    source_uri: str
    document_type: DocumentType
    media_type: str | None
    sha256: str | None
    size_bytes: int | None
    original_path: str | None
    category: DocumentCategory = "other"
    pages: list[DocumentPage] = field(default_factory=list)
    tables: list[DocumentTable] = field(default_factory=list)
    images: list[DocumentImage] = field(default_factory=list)
    ocr_status: Literal["not_requested", "pending", "completed", "failed"] = "not_requested"
    warnings: list[ParseWarning] = field(default_factory=list)
    extracted_text: str = ""
    model_extraction: ModelExtraction = field(default_factory=ModelExtraction)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
