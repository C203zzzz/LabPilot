"""M11 report, export, and immutable version contracts.

The contracts keep report conclusions small and auditable: every report item
either carries evidence references or states why no evidence reference can be
provided.  Version metadata is separate from report content so the same input
can be recognised as idempotent without relying on timestamps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from backend.domain.errors import ModuleValidationError


class SelectionStatus(StrEnum):
    RECOMMENDATIONS = "recommendations"
    NO_RECOMMENDATION = "no_recommendation"


class ExportFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModuleValidationError(f"{field_name} is required", code="invalid_input", field=field_name)
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class EvidenceReference:
    """A stable pointer to the evidence behind one report statement.

    At least one of fact_id, evidence_id, or quote_id must be retained.  A
    document/page, URL/body location, or quote source location is required
    unless ``missing_reason`` explicitly explains why the pointer is
    incomplete.
    """

    fact_id: str | None = None
    evidence_id: str | None = None
    candidate_id: str | None = None
    document_id: str | None = None
    page: int | None = None
    location: str | None = None
    url: str | None = None
    body_location: str | None = None
    quote: str | None = None
    source_type: str | None = None
    price_evidence_level: str | None = None
    tender_title: str | None = None
    publication_date: str | None = None
    currency: str | None = None
    tax_condition: str | None = None
    price_amount: float | None = None
    price_unit: str | None = None
    price_quote: str | None = None
    quote_id: str | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "fact_id", "evidence_id", "candidate_id", "document_id", "location",
            "url", "body_location", "quote", "source_type", "price_evidence_level",
            "tender_title", "publication_date", "currency", "tax_condition",
            "price_unit", "price_quote",
            "quote_id", "missing_reason",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name)))
        if self.page is not None:
            try:
                object.__setattr__(self, "page", int(self.page))
            except (TypeError, ValueError) as exc:
                raise ModuleValidationError("page must be an integer", code="invalid_input", field="page") from exc
        has_pointer = bool(any((self.fact_id, self.evidence_id, self.quote_id, self.document_id, self.url)))
        if not has_pointer and not self.missing_reason:
            raise ModuleValidationError(
                "evidence reference requires an id, document_id, URL, or missing reason",
                code="missing_evidence_reference",
                field="evidence",
            )
        has_location = bool(
            (self.document_id and (self.location or self.page is not None)) or
            (self.url and (self.body_location or self.location)) or
            (self.quote_id and self.location)
        )
        if not has_location and not self.missing_reason:
            raise ModuleValidationError(
                "evidence reference requires document/page, URL/body location, or quote location",
                code="missing_evidence_location",
                field="evidence",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "document_id": self.document_id,
            "page": self.page,
            "location": self.location,
            "url": self.url,
            "body_location": self.body_location,
            "quote": self.quote,
            "source_type": self.source_type,
            "price_evidence_level": self.price_evidence_level,
            "tender_title": self.tender_title,
            "publication_date": self.publication_date,
            "currency": self.currency,
            "tax_condition": self.tax_condition,
            "price_amount": self.price_amount,
            "price_unit": self.price_unit,
            "price_quote": self.price_quote,
            "quote_id": self.quote_id,
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class ReportItem:
    """One auditable statement inside a report section."""

    label: str
    text: str
    candidate_id: str | None = None
    evidence: tuple[EvidenceReference, ...] = ()
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "text", _text(self.text, "text"))
        object.__setattr__(self, "candidate_id", _optional_text(self.candidate_id))
        object.__setattr__(self, "missing_reason", _optional_text(self.missing_reason))
        refs: list[EvidenceReference] = []
        for reference in self.evidence:
            if isinstance(reference, EvidenceReference):
                refs.append(reference)
            elif isinstance(reference, dict):
                refs.append(EvidenceReference(**reference))
            else:
                raise ModuleValidationError("evidence must contain EvidenceReference values", code="invalid_input", field="evidence")
        object.__setattr__(self, "evidence", tuple(refs))
        if not refs and not self.missing_reason:
            raise ModuleValidationError(
                "report item must carry evidence or explain why evidence is missing",
                code="missing_evidence_reason",
                field="evidence",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.text,
            "candidate_id": self.candidate_id,
            "evidence": [reference.to_dict() for reference in self.evidence],
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class ReportSection:
    key: str
    title: str
    summary: str
    items: tuple[ReportItem, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, "key"))
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "summary", _text(self.summary, "summary"))
        object.__setattr__(self, "missing_reason", _optional_text(self.missing_reason))
        items: list[ReportItem] = []
        for item in self.items:
            if isinstance(item, ReportItem):
                items.append(item)
            elif isinstance(item, dict):
                items.append(ReportItem(**item))
            else:
                raise ModuleValidationError("items must contain ReportItem values", code="invalid_input", field="items")
        object.__setattr__(self, "items", tuple(items))
        refs: list[EvidenceReference] = []
        for reference in self.evidence:
            if isinstance(reference, EvidenceReference):
                refs.append(reference)
            elif isinstance(reference, dict):
                refs.append(EvidenceReference(**reference))
            else:
                raise ModuleValidationError("evidence must contain EvidenceReference values", code="invalid_input", field="evidence")
        object.__setattr__(self, "evidence", tuple(refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
            "evidence": [reference.to_dict() for reference in self.evidence],
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class SelectionReport:
    task_id: str
    status: SelectionStatus | str
    sections: tuple[ReportSection, ...]
    preferred: dict[str, Any] | None = None
    alternatives: tuple[dict[str, Any], ...] = ()
    needs_review: tuple[dict[str, Any], ...] = ()
    ineligible: tuple[dict[str, Any], ...] = ()
    supplier_questions: tuple[dict[str, Any], ...] = ()
    report_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        try:
            object.__setattr__(self, "status", SelectionStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("status must be recommendations or no_recommendation", code="invalid_input", field="status") from exc
        object.__setattr__(self, "report_id", _optional_text(self.report_id) or "")
        object.__setattr__(self, "content_hash", _optional_text(self.content_hash) or "")
        normalized_sections: list[ReportSection] = []
        for section in self.sections:
            if isinstance(section, ReportSection):
                normalized_sections.append(section)
            elif isinstance(section, dict):
                normalized_sections.append(ReportSection(**section))
            else:
                raise ModuleValidationError("sections must contain ReportSection values", code="invalid_input", field="sections")
        object.__setattr__(self, "sections", tuple(normalized_sections))
        object.__setattr__(self, "alternatives", tuple(dict(item) for item in self.alternatives))
        object.__setattr__(self, "needs_review", tuple(dict(item) for item in self.needs_review))
        object.__setattr__(self, "ineligible", tuple(dict(item) for item in self.ineligible))
        object.__setattr__(self, "supplier_questions", tuple(dict(item) for item in self.supplier_questions))

    def section(self, key: str) -> ReportSection | None:
        return next((section for section in self.sections if section.key == key), None)

    @property
    def by_section(self) -> dict[str, ReportSection]:
        return {section.key: section for section in self.sections}

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "preferred": self.preferred,
            "alternatives": list(self.alternatives),
            "needs_review": list(self.needs_review),
            "ineligible": list(self.ineligible),
            "sections": [section.to_dict() for section in self.sections],
            "supplier_questions": list(self.supplier_questions),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ReportVersion:
    task_id: str
    version_number: int
    parent_version_id: str | None
    revision_reason: str
    created_at: datetime
    content_hash: str
    version_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "revision_reason", _text(self.revision_reason, "revision_reason"))
        object.__setattr__(self, "content_hash", _text(self.content_hash, "content_hash"))
        object.__setattr__(self, "version_id", _optional_text(self.version_id) or "")
        object.__setattr__(self, "parent_version_id", _optional_text(self.parent_version_id))
        try:
            version_number = int(self.version_number)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("version_number must be a positive integer", code="invalid_version", field="version_number") from exc
        if version_number < 1:
            raise ModuleValidationError("version_number must be a positive integer", code="invalid_version", field="version_number")
        object.__setattr__(self, "version_number", version_number)
        if isinstance(self.created_at, str):
            try:
                object.__setattr__(self, "created_at", datetime.fromisoformat(self.created_at))
            except ValueError as exc:
                raise ModuleValidationError("created_at must be ISO-8601", code="invalid_created_at", field="created_at") from exc
        if not isinstance(self.created_at, datetime):
            raise ModuleValidationError("created_at must be datetime", code="invalid_created_at", field="created_at")
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "task_id": self.task_id,
            "version_number": self.version_number,
            "parent_version_id": self.parent_version_id,
            "revision_reason": self.revision_reason,
            "created_at": self.created_at.isoformat(),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ReportSnapshot:
    version: ReportVersion
    report: SelectionReport

    def __post_init__(self) -> None:
        if not isinstance(self.version, ReportVersion) or not isinstance(self.report, SelectionReport):
            raise ModuleValidationError("snapshot requires ReportVersion and SelectionReport", code="invalid_snapshot", field="snapshot")
        if self.version.task_id != self.report.task_id:
            raise ModuleValidationError("version and report task_id mismatch", code="association_mismatch", field="task_id")
        if self.version.content_hash != self.report.content_hash:
            raise ModuleValidationError("version and report content_hash mismatch", code="hash_mismatch", field="content_hash")

    @property
    def task_id(self) -> str:
        return self.version.task_id

    @property
    def version_number(self) -> int:
        return self.version.version_number

    @property
    def version_id(self) -> str:
        return self.version.version_id

    @property
    def content_hash(self) -> str:
        return self.version.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version.to_dict(), "report": self.report.to_dict()}


@dataclass(frozen=True)
class ExportResult:
    format: ExportFormat | str
    content: str
    filename: str
    content_hash: str
    report_id: str
    version_number: int | None = None
    media_type: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "format", ExportFormat(self.format))
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("format must be markdown or json", code="invalid_export_format", field="format") from exc
        if not isinstance(self.content, str):
            raise ModuleValidationError("content must be text", code="invalid_input", field="content")
        object.__setattr__(self, "filename", _text(self.filename, "filename"))
        object.__setattr__(self, "content_hash", _text(self.content_hash, "content_hash"))
        object.__setattr__(self, "report_id", _text(self.report_id, "report_id"))
        if self.version_number is not None:
            try:
                version_number = int(self.version_number)
            except (TypeError, ValueError) as exc:
                raise ModuleValidationError("version_number must be positive", code="invalid_version", field="version_number") from exc
            if version_number < 1:
                raise ModuleValidationError("version_number must be positive", code="invalid_version", field="version_number")
            object.__setattr__(self, "version_number", version_number)
        if not self.media_type:
            object.__setattr__(self, "media_type", "text/markdown; charset=utf-8" if self.format is ExportFormat.MARKDOWN else "application/json; charset=utf-8")

    @property
    def export_format(self) -> ExportFormat:
        return self.format

    @property
    def data(self) -> str:
        return self.content

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "content": self.content,
            "filename": self.filename,
            "media_type": self.media_type,
            "content_hash": self.content_hash,
            "report_id": self.report_id,
            "version_number": self.version_number,
        }


@runtime_checkable
class ReportRepository(Protocol):
    def save(self, snapshot: ReportSnapshot, expected_parent_version_id: str | None = None) -> ReportSnapshot: ...
    def get(self, task_id: str, version_number: int) -> ReportSnapshot | None: ...
    def list_versions(self, task_id: str) -> list[ReportSnapshot]: ...
    def latest(self, task_id: str) -> ReportSnapshot | None: ...


class InMemoryReportRepository:
    """Immutable-history repository suitable for the first offline version."""

    def __init__(self) -> None:
        self._snapshots: dict[str, list[ReportSnapshot]] = {}

    def save(self, snapshot: ReportSnapshot, expected_parent_version_id: str | None = None) -> ReportSnapshot:
        if not isinstance(snapshot, ReportSnapshot):
            raise ModuleValidationError("snapshot must be ReportSnapshot", code="invalid_snapshot", field="snapshot")
        task_id = snapshot.version.task_id
        versions = self._snapshots.setdefault(task_id, [])
        if versions:
            current = versions[-1]
            if current.version.content_hash == snapshot.version.content_hash:
                if expected_parent_version_id not in (None, current.version.version_id):
                    raise ModuleValidationError(
                        "parent_version_id does not match the current version",
                        code="version_conflict",
                        field="parent_version_id",
                    )
                return _copy_snapshot(current)
            if expected_parent_version_id != current.version.version_id:
                raise ModuleValidationError(
                    "parent_version_id does not match the current version",
                    code="version_conflict",
                    field="parent_version_id",
                )
            if snapshot.version.parent_version_id != current.version.version_id:
                raise ModuleValidationError("snapshot parent_version_id is stale", code="version_conflict", field="parent_version_id")
            if snapshot.version.version_number != current.version.version_number + 1:
                raise ModuleValidationError("version_number must increase by one", code="version_conflict", field="version_number")
            if any(existing.version.version_number == snapshot.version.version_number for existing in versions):
                raise ModuleValidationError("version_number already exists", code="version_conflict", field="version_number")
        else:
            if expected_parent_version_id is not None:
                raise ModuleValidationError("no parent version exists", code="version_conflict", field="parent_version_id")
            if snapshot.version.parent_version_id is not None or snapshot.version.version_number != 1:
                raise ModuleValidationError("first version must have version_number 1 and no parent", code="version_conflict", field="version_number")
        versions.append(_copy_snapshot(snapshot))
        return _copy_snapshot(snapshot)

    def get(self, task_id: str, version_number: int) -> ReportSnapshot | None:
        _text(task_id, "task_id")
        try:
            number = int(version_number)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("version_number must be an integer", code="invalid_version", field="version_number") from exc
        for snapshot in self._snapshots.get(task_id, []):
            if snapshot.version.version_number == number:
                return _copy_snapshot(snapshot)
        return None

    def list_versions(self, task_id: str) -> list[ReportSnapshot]:
        _text(task_id, "task_id")
        return [_copy_snapshot(snapshot) for snapshot in self._snapshots.get(task_id, [])]

    def latest(self, task_id: str) -> ReportSnapshot | None:
        _text(task_id, "task_id")
        versions = self._snapshots.get(task_id, [])
        return _copy_snapshot(versions[-1]) if versions else None

    def history(self, task_id: str) -> list[ReportSnapshot]:
        return self.list_versions(task_id)

    def get_version(self, task_id: str, version_number: int) -> ReportSnapshot | None:
        return self.get(task_id, version_number)


class SQLiteReportRepository:
    """SQLite adapter boundary only; M11 does not add migrations or persistence."""

    def __init__(self, connection: Any | None = None, *, table_prefix: str = "selection_report") -> None:
        self.connection = connection
        self.table_prefix = _text(table_prefix, "table_prefix")

    def save(self, snapshot: ReportSnapshot, expected_parent_version_id: str | None = None) -> ReportSnapshot:
        raise NotImplementedError("SQLite report persistence is outside the M11 first-version scope")

    def get(self, task_id: str, version_number: int) -> ReportSnapshot | None:
        raise NotImplementedError("SQLite report persistence is outside the M11 first-version scope")

    def list_versions(self, task_id: str) -> list[ReportSnapshot]:
        raise NotImplementedError("SQLite report persistence is outside the M11 first-version scope")

    def latest(self, task_id: str) -> ReportSnapshot | None:
        raise NotImplementedError("SQLite report persistence is outside the M11 first-version scope")


def _copy_snapshot(snapshot: ReportSnapshot) -> ReportSnapshot:
    """Deep-copy through the deterministic JSON representation."""
    import json

    return snapshot_from_dict(json.loads(json.dumps(snapshot.to_dict(), ensure_ascii=False)))


def snapshot_from_dict(data: dict[str, Any]) -> ReportSnapshot:
    if not isinstance(data, dict):
        raise ModuleValidationError("snapshot must be a JSON object", code="invalid_snapshot", field="snapshot")
    version_data = data.get("version")
    report_data = data.get("report")
    if not isinstance(version_data, dict) or not isinstance(report_data, dict):
        raise ModuleValidationError("snapshot requires version and report objects", code="invalid_snapshot", field="snapshot")
    version = ReportVersion(
        task_id=version_data.get("task_id", ""),
        version_number=version_data.get("version_number", 0),
        parent_version_id=version_data.get("parent_version_id"),
        revision_reason=version_data.get("revision_reason", ""),
        created_at=version_data.get("created_at"),
        content_hash=version_data.get("content_hash", ""),
        version_id=version_data.get("version_id", ""),
    )
    report = _report_from_dict(report_data)
    return ReportSnapshot(version=version, report=report)


def _report_from_dict(data: dict[str, Any]) -> SelectionReport:
    sections = []
    for raw in data.get("sections", []) or []:
        items = [ReportItem(**item) for item in raw.get("items", []) or []]
        evidence = [EvidenceReference(**reference) for reference in raw.get("evidence", []) or []]
        sections.append(ReportSection(
            key=raw.get("key", ""), title=raw.get("title", ""),
            summary=raw.get("summary", ""), items=tuple(items), evidence=tuple(evidence),
            missing_reason=raw.get("missing_reason"),
        ))
    return SelectionReport(
        task_id=data.get("task_id", ""), status=data.get("status", ""),
        preferred=data.get("preferred"), alternatives=tuple(data.get("alternatives", []) or []),
        needs_review=tuple(data.get("needs_review", []) or []),
        ineligible=tuple(data.get("ineligible", []) or []),
        supplier_questions=tuple(data.get("supplier_questions", []) or []),
        sections=tuple(sections), report_id=data.get("report_id", ""),
        content_hash=data.get("content_hash", ""),
    )


__all__ = [
    "SelectionStatus", "ExportFormat", "EvidenceReference", "ReportItem",
    "ReportSection", "SelectionReport", "ReportVersion", "ReportSnapshot",
    "ExportResult", "ReportRepository", "InMemoryReportRepository",
    "SQLiteReportRepository", "snapshot_from_dict",
]
