"""M04 official-search domain contracts and deterministic helpers (immutable)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class SourceType(str, Enum):
    MANUFACTURER_WEBSITE = "manufacturer_website"
    MANUFACTURER_DOWNLOAD = "manufacturer_download"
    DEALER = "dealer"
    AGGREGATOR = "aggregator"
    SEARCH_SNIPPET = "search_snippet"
    UNKNOWN = "unknown"
    TENDER_DOCUMENT = "tender_document"


class PriceEvidenceLevel(str, Enum):
    HISTORICAL_TENDER = "historical_tender"
    OFFICIAL_PUBLIC = "official_public"
    DEALER_LEAD = "dealer_lead"
    UNKNOWN = "unknown"


class BodyStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    FETCHED = "fetched"
    FAILED = "failed"
    TIMEOUT = "timeout"


_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "_ga", "_gid",
})


def normalize_url(url: str) -> str:
    """Normalize HTTP(S) URLs, stripping tracking params and rejecting credentials."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("invalid URL: empty")
    raw = url.strip()
    if len(raw) > 2048:
        raise ValueError(f"URL too long: {len(raw)} chars (max 2048)")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"invalid URL scheme: {parts.scheme} (must be http/https)")
    if not parts.netloc:
        raise ValueError("invalid URL: missing domain")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("invalid URL: empty hostname")
    port = parts.port
    if (
        port is None
        or (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    if parts.username or parts.password:
        raise ValueError("URL credentials are not allowed")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))


_LEAD_TEXT_FIELD_LIMIT = 200


def _normalize_lead_identifier(value: str | None, field_name: str) -> str | None:
    """线索标识字段（买方/卖方/项目编号）：可选，缺省表示"不约束"。

    归一化规则：None、空串、纯空白一律折叠成 None，其余去掉首尾空白后返回。
    超过 200 字符按异常数据处理，直接报错。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a str, got {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > _LEAD_TEXT_FIELD_LIMIT:
        raise ValueError(f"{field_name} too long: {len(cleaned)} chars (max {_LEAD_TEXT_FIELD_LIMIT})")
    return cleaned


def _normalize_window_bound(value: str | None, field_name: str) -> str | None:
    """查询窗口边界：可选，None 表示该侧不设限；给值必须是零填充 ISO 日期 YYYY-MM-DD。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date string (YYYY-MM-DD), got {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO date string (YYYY-MM-DD), got {value!r}") from None
    if parsed.date().isoformat() != cleaned:
        raise ValueError(f"{field_name} must be a zero-padded ISO date (YYYY-MM-DD), got {value!r}")
    return cleaned


@dataclass(frozen=True)
class SearchQuery:
    text: str
    candidate_id: str | None = None
    brand: str | None = None
    model: str | None = None
    limit: int = 10
    purpose: str = "general"
    target_source_types: tuple[SourceType, ...] = ()
    # 线索对齐字段（设计规范 对话 1）。全部可选且带默认值，老调用点不传也能构造。
    # 注意本对象用 supplier_name，SourceRecord 用 supplier。
    buyer_name: str | None = None
    supplier_name: str | None = None
    project_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query text cannot be empty")
        if len(self.text) > 500:
            raise ValueError(f"query text too long: {len(self.text)} chars (max 500)")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            raise ValueError(f"limit must be 1-100, got {self.limit}")
        if self.brand and len(self.brand) > 100:
            raise ValueError(f"brand too long: {len(self.brand)} chars")
        if self.model and len(self.model) > 100:
            raise ValueError(f"model too long: {len(self.model)} chars")
        for name in ("buyer_name", "supplier_name", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        for name in ("window_start", "window_end"):
            object.__setattr__(self, name, _normalize_window_bound(getattr(self, name), name))
        if self.window_start and self.window_end and self.window_start > self.window_end:
            raise ValueError(f"window_start must not be later than window_end: {self.window_start} > {self.window_end}")
        if isinstance(self.target_source_types, list):
            object.__setattr__(self, "target_source_types", tuple(self.target_source_types))


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    provider: str = "unknown"
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class SourceRecord:
    url: str
    title: str
    source_type: SourceType
    authority_score: int
    authority_reason: str
    snippet: str = ""
    body_text: str | None = None
    body_status: BodyStatus = BodyStatus.NOT_REQUESTED
    crawled_at: datetime | None = None
    error_reason: str | None = None
    provider: str = "unknown"
    provider_provenance: tuple[str, ...] = ()
    price_evidence_level: PriceEvidenceLevel = PriceEvidenceLevel.UNKNOWN
    tender_title: str | None = None
    publication_date: str | None = None
    currency: str | None = None
    tax_condition: str | None = None
    source_location: str | None = None
    price_amount: float | None = None
    price_unit: str | None = None
    price_quote: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 线索对齐字段（设计规范 对话 1）。刻意放在字段表末尾以兼容位置参数构造。
    supplier: str | None = None
    buyer: str | None = None
    project_id: str | None = None
    quantity: float | None = None

    def __post_init__(self) -> None:
        try:
            normalized = normalize_url(self.url)
            object.__setattr__(self, "url", normalized)
        except ValueError as exc:
            raise ValueError(f"invalid source URL: {exc}") from exc
        if isinstance(self.authority_score, bool) or not isinstance(self.authority_score, int) or not 0 <= self.authority_score <= 100:
            raise ValueError(f"authority_score must be 0-100, got {self.authority_score}")
        if not isinstance(self.source_type, SourceType):
            raise ValueError(f"source_type must be SourceType, got {type(self.source_type)}")
        if not isinstance(self.body_status, BodyStatus):
            raise ValueError(f"body_status must be BodyStatus, got {type(self.body_status)}")
        if not isinstance(self.price_evidence_level, PriceEvidenceLevel):
            raise ValueError(f"price_evidence_level must be PriceEvidenceLevel, got {type(self.price_evidence_level)}")
        if self.body_text and len(self.body_text) > 64 * 1024 * 1024:
            raise ValueError(f"body_text too large: {len(self.body_text)} bytes")
        if self.price_amount is not None:
            if isinstance(self.price_amount, bool) or not isinstance(self.price_amount, (int, float)) or self.price_amount < 0:
                raise ValueError(f"price_amount must be non-negative, got {self.price_amount}")
        for name in ("supplier", "buyer", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        if self.quantity is not None:
            if isinstance(self.quantity, bool) or not isinstance(self.quantity, (int, float)) or self.quantity < 0:
                raise ValueError(f"quantity must be non-negative, got {self.quantity}")
        if isinstance(self.provider_provenance, list):
            object.__setattr__(self, "provider_provenance", tuple(self.provider_provenance))

    def with_body(self, body_text: str, crawled_at: datetime | None = None) -> SourceRecord:
        if len(body_text) > 64 * 1024 * 1024:
            raise ValueError(f"body_text too large: {len(body_text)} bytes")
        return replace(
            self,
            body_text=body_text,
            body_status=BodyStatus.FETCHED,
            crawled_at=crawled_at or datetime.now(timezone.utc),
        )

    def with_error(self, error_reason: str, is_timeout: bool = False) -> SourceRecord:
        return replace(
            self,
            error_reason=error_reason,
            body_status=BodyStatus.TIMEOUT if is_timeout else BodyStatus.FAILED,
        )


class SearchProvider(Protocol):
    name: str
    def search(self, query: SearchQuery) -> Sequence[SearchResult]: ...
    def supports(self, query: SearchQuery) -> bool: ...


__all__ = [
    "SourceType",
    "PriceEvidenceLevel",
    "BodyStatus",
    "normalize_url",
    "SearchQuery",
    "SearchResult",
    "SourceRecord",
    "SearchProvider",
]
