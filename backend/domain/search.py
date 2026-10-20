"""M04 official-search domain contracts and deterministic helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
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


def normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("invalid URL: empty")
    raw = url.strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"invalid URL: {url}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"invalid URL: {url}")
    port = parts.port
    netloc = host if not port or (parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443) else f"{host}:{port}"
    if parts.username or parts.password:
        raise ValueError("URL credentials are not allowed")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))


_LEAD_TEXT_FIELD_LIMIT = 200


def _normalize_lead_identifier(value: str | None, field_name: str) -> str | None:
    """线索标识字段（买方/卖方/项目编号）：可选，缺省表示"不约束"。

    归一化规则：None、空串、纯空白一律折叠成 None，其余去掉首尾空白后返回。
    这么定是为了对接聚合站抓来的脏值——下游用 ``if query.buyer_name:`` 判断即可，
    不必到处 strip，也不会因为拿到一个空串就炸掉整条链路。
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
    """查询窗口边界：可选，None 表示该侧不设限；给值必须是零填充 ISO 日期 YYYY-MM-DD。

    强制零填充是为了让窗口边界能直接和 ``SourceRecord.publication_date``
    做字符串字典序比较，对齐时不必再解析。空串同样折叠成 None。
    """
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
    # 线索对齐字段（对话 1）。全部可选且带默认值，老调用点不传也能构造。
    # 日期统一为零填充 ISO YYYY-MM-DD 字符串，可直接拼进 CCGP 的
    # start_time/end_time。注意本对象用 supplier_name，SourceRecord 用 supplier。
    buyer_name: str | None = None
    supplier_name: str | None = None
    project_id: str | None = None
    window_start: str | None = None
    window_end: str | None = None

    def __post_init__(self):
        if not self.text.strip():
            raise ValueError("query text cannot be empty")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        for name in ("buyer_name", "supplier_name", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        for name in ("window_start", "window_end"):
            object.__setattr__(self, name, _normalize_window_bound(getattr(self, name), name))
        if self.window_start and self.window_end and self.window_start > self.window_end:
            raise ValueError(f"window_start must not be later than window_end: {self.window_start} > {self.window_end}")


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    provider: str = "unknown"
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
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
    provider_provenance: list[str] = field(default_factory=list)
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
    # 线索对齐字段（对话 1）。全部可选且带默认值，并且刻意放在字段表
    # 末尾：official_search.py 用位置参数构造本对象
    # （url, title, source_type, authority_score, authority_reason, snippet），
    # 插在中间会让那些调用点错位。注意本对象用 supplier，SearchQuery 用 supplier_name。
    supplier: str | None = None
    buyer: str | None = None
    project_id: str | None = None
    quantity: float | None = None

    def __post_init__(self):
        # 只校验本次新增的线索对齐字段；不触碰任何既有字段的既有行为。
        for name in ("supplier", "buyer", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        if self.quantity is not None:
            if not isinstance(self.quantity, (int, float)) or self.quantity < 0:
                raise ValueError(f"quantity must be non-negative, got {self.quantity}")


class SearchProvider(Protocol):
    name: str
    def search(self, query: SearchQuery) -> Sequence[SearchResult]: ...
    def supports(self, query: SearchQuery) -> bool: ...
