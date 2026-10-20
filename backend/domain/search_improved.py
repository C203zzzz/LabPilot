"""第一性原理优化：search领域模型

修复P0.1：不可变性
修复P0.2：输入校验
修复P1.2：资源限制
"""
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


def normalize_url(url: str) -> str:
    """规范化URL，确保一致性

    校验规则：
    1. 必须是http/https
    2. 不能包含认证信息
    3. 移除tracking参数
    4. 统一端口表示
    5. 长度限制
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("invalid URL: empty")

    raw = url.strip()

    # 长度限制
    if len(raw) > 2048:
        raise ValueError(f"URL too long: {len(raw)} chars (max 2048)")

    parts = urlsplit(raw)

    # 协议校验
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"invalid URL scheme: {parts.scheme} (must be http/https)")

    # 域名校验
    if not parts.netloc:
        raise ValueError(f"invalid URL: missing domain")

    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"invalid URL: empty hostname")

    # 端口处理
    port = parts.port
    if port is None or \
       (parts.scheme.lower() == "http" and port == 80) or \
       (parts.scheme.lower() == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"

    # 不允许认证信息
    if parts.username or parts.password:
        raise ValueError("URL credentials are not allowed")

    # 移除tracking参数
    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "msclkid", "_ga", "_gid"
    }
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in tracking_params
    ]

    # 路径标准化
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    return urlunsplit((
        parts.scheme.lower(),
        netloc,
        path,
        urlencode(sorted(query)),
        ""
    ))


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
    """不可变的搜索查询对象"""
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
        """输入校验"""
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query text cannot be empty")
        if len(self.text) > 500:
            raise ValueError(f"query text too long: {len(self.text)} chars (max 500)")

        if not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            raise ValueError(f"limit must be 1-100, got {self.limit}")

        if self.brand and len(self.brand) > 100:
            raise ValueError(f"brand too long: {len(self.brand)} chars")

        if self.model and len(self.model) > 100:
            raise ValueError(f"model too long: {len(self.model)} chars")

        # 线索对齐字段校验 + 归一化
        for name in ("buyer_name", "supplier_name", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        for name in ("window_start", "window_end"):
            object.__setattr__(self, name, _normalize_window_bound(getattr(self, name), name))
        if self.window_start and self.window_end and self.window_start > self.window_end:
            raise ValueError(f"window_start must not be later than window_end: {self.window_start} > {self.window_end}")

        # 转换为不可变类型
        if isinstance(self.target_source_types, list):
            object.__setattr__(self, 'target_source_types', tuple(self.target_source_types))


@dataclass(frozen=True)
class SearchResult:
    """不可变的搜索结果对象"""
    url: str
    title: str
    snippet: str = ""
    provider: str = "unknown"
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """输入校验"""
        # URL规范化
        try:
            normalized = normalize_url(self.url)
            object.__setattr__(self, 'url', normalized)
        except ValueError as e:
            raise ValueError(f"invalid result URL: {e}")

        if len(self.title) > 500:
            raise ValueError(f"title too long: {len(self.title)} chars")

        if len(self.snippet) > 2000:
            raise ValueError(f"snippet too long: {len(self.snippet)} chars")


@dataclass(frozen=True)
class SourceRecord:
    """不可变的来源记录对象

    注意：body_text可能很大，谨慎使用
    """
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
    provider_provenance: tuple[str, ...] = ()  # 不可变
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
        """输入校验"""
        # URL规范化
        try:
            normalized = normalize_url(self.url)
            object.__setattr__(self, 'url', normalized)
        except ValueError as e:
            raise ValueError(f"invalid source URL: {e}")

        # authority_score范围
        if not isinstance(self.authority_score, int) or not 0 <= self.authority_score <= 100:
            raise ValueError(f"authority_score must be 0-100, got {self.authority_score}")

        # 类型校验
        if not isinstance(self.source_type, SourceType):
            raise ValueError(f"source_type must be SourceType, got {type(self.source_type)}")

        if not isinstance(self.body_status, BodyStatus):
            raise ValueError(f"body_status must be BodyStatus, got {type(self.body_status)}")

        if not isinstance(self.price_evidence_level, PriceEvidenceLevel):
            raise ValueError(f"price_evidence_level must be PriceEvidenceLevel, got {type(self.price_evidence_level)}")

        # body_text大小限制
        if self.body_text and len(self.body_text) > 64 * 1024 * 1024:  # 64MB
            raise ValueError(f"body_text too large: {len(self.body_text)} bytes")

        # price_amount校验
        if self.price_amount is not None:
            if not isinstance(self.price_amount, (int, float)) or self.price_amount < 0:
                raise ValueError(f"price_amount must be non-negative, got {self.price_amount}")

        # 线索对齐字段校验 + 归一化
        for name in ("supplier", "buyer", "project_id"):
            object.__setattr__(self, name, _normalize_lead_identifier(getattr(self, name), name))
        if self.quantity is not None:
            if not isinstance(self.quantity, (int, float)) or self.quantity < 0:
                raise ValueError(f"quantity must be non-negative, got {self.quantity}")

        # 转换为不可变类型
        if isinstance(self.provider_provenance, list):
            object.__setattr__(self, 'provider_provenance', tuple(self.provider_provenance))

    def with_body(
        self,
        body_text: str,
        crawled_at: datetime | None = None
    ) -> SourceRecord:
        """返回新对象，添加body"""
        if len(body_text) > 64 * 1024 * 1024:
            raise ValueError(f"body_text too large: {len(body_text)} bytes")

        return replace(
            self,
            body_text=body_text,
            body_status=BodyStatus.FETCHED,
            crawled_at=crawled_at or datetime.now(timezone.utc)
        )

    def with_error(self, error_reason: str, is_timeout: bool = False) -> SourceRecord:
        """返回新对象，标记错误"""
        return replace(
            self,
            error_reason=error_reason,
            body_status=BodyStatus.TIMEOUT if is_timeout else BodyStatus.FAILED
        )


class SearchProvider(Protocol):
    """搜索提供者接口"""
    name: str

    def search(self, query: SearchQuery) -> Sequence[SearchResult]:
        """执行搜索"""
        ...

    def supports(self, query: SearchQuery) -> bool:
        """是否支持该查询"""
        ...


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
