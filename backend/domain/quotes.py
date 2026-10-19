"""M09 supplier quote and procurement comparison contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
import json
import re
from dataclasses import asdict, is_dataclass

from backend.domain.errors import ModuleValidationError


class QuoteSourceType(StrEnum):
    CURRENT_SUPPLIER_QUOTE = "current_supplier_quote"
    OFFICIAL_PUBLIC_PRICE = "official_public_price"
    HISTORICAL_TENDER_PRICE = "historical_tender_price"
    DEALER_LEAD = "dealer_lead"
    UNKNOWN = "unknown"


class QuoteConditionType(StrEnum):
    TAX = "tax"
    VALIDITY = "validity"
    DELIVERY = "delivery"
    INSTALLATION = "installation"
    WARRANTY = "warranty"
    AFTER_SALES = "after_sales"
    PAYMENT = "payment"
    OTHER = "other"


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModuleValidationError(f"{field_name} is required", code="invalid_quote", field=field_name)
    return value.strip()


@dataclass(frozen=True)
class QuoteCondition:
    condition_type: QuoteConditionType | str
    value: Any = "unknown"
    source_location: str = "unknown"
    note: str | None = None

    def __post_init__(self):
        try:
            object.__setattr__(self, "condition_type", QuoteConditionType(self.condition_type))
        except (ValueError, TypeError) as exc:
            raise ModuleValidationError("invalid condition_type", code="invalid_condition", field="condition_type") from exc
        if self.value is None or (isinstance(self.value, str) and not self.value.strip()):
            object.__setattr__(self, "value", "unknown")
        object.__setattr__(self, "source_location", _required(self.source_location, "source_location"))


@dataclass(frozen=True)
class SupplierQuote:
    quote_id: str
    candidate_id: str
    supplier: str
    model: str
    configuration: Any
    amount: int | float | str
    currency: str
    source_type: QuoteSourceType | str
    source: str
    source_location: str
    quote_date: date | datetime | str | None = None
    valid_until: date | datetime | str | None = None
    conditions: tuple[QuoteCondition, ...] = field(default_factory=tuple)
    note: str | None = None

    def __post_init__(self):
        for name in ("quote_id", "candidate_id", "supplier", "model", "source", "source_location"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        try:
            amount = float(self.amount)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("amount must be numeric", code="invalid_amount", field="amount") from exc
        if amount < 0 or amount != amount:
            raise ModuleValidationError("amount must be non-negative", code="invalid_amount", field="amount")
        object.__setattr__(self, "amount", amount)
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency.upper()):
            raise ModuleValidationError("currency must be a 3-letter ISO code", code="invalid_currency", field="currency")
        object.__setattr__(self, "currency", self.currency.upper())
        try:
            object.__setattr__(self, "source_type", QuoteSourceType(self.source_type))
        except (ValueError, TypeError) as exc:
            raise ModuleValidationError("invalid source_type", code="invalid_source_type", field="source_type") from exc
        if self.source_type == QuoteSourceType.UNKNOWN and self.source_location.lower().startswith("snippet:"):
            raise ModuleValidationError("search snippets cannot form a quote", code="snippet_quote_forbidden", field="source_location")
        normalized = [c if isinstance(c, QuoteCondition) else QuoteCondition(**c) for c in self.conditions]
        present = {c.condition_type for c in normalized}
        for kind in (QuoteConditionType.TAX, QuoteConditionType.DELIVERY, QuoteConditionType.INSTALLATION,
                     QuoteConditionType.WARRANTY, QuoteConditionType.AFTER_SALES, QuoteConditionType.PAYMENT):
            if kind not in present:
                normalized.append(QuoteCondition(kind, "unknown", self.source_location))
        object.__setattr__(self, "conditions", tuple(normalized))

    def to_dict(self) -> dict[str, Any]:
        return quote_to_dict(self)

    @property
    def configuration_key(self) -> str:
        return json.dumps(self.configuration, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PriceComparison:
    quote_ids: tuple[str, ...]
    comparable: bool
    reason: str
    amount_delta: float | None = None
    gaps: tuple[str, ...] = ()

    def to_dict(self):
        return {"quote_ids": list(self.quote_ids), "comparable": self.comparable, "reason": self.reason,
                "amount_delta": self.amount_delta, "gaps": list(self.gaps)}


@dataclass(frozen=True)
class ProcurementComparison:
    candidate_id: str
    quotes: tuple[SupplierQuote, ...]
    comparable: tuple[PriceComparison, ...] = ()
    incomparable: tuple[PriceComparison, ...] = ()
    gaps: tuple[str, ...] = ()

    def to_dict(self):
        return {"candidate_id": self.candidate_id, "quotes": [q.to_dict() for q in self.quotes],
                "comparable": [x.to_dict() for x in self.comparable],
                "incomparable": [x.to_dict() for x in self.incomparable], "gaps": list(self.gaps)}


def quote_to_dict(value: SupplierQuote) -> dict[str, Any]:
    def conv(v):
        if isinstance(v, StrEnum): return v.value
        if isinstance(v, (date, datetime)): return v.isoformat()
        if is_dataclass(v): return conv(asdict(v))
        if isinstance(v, tuple): return [conv(x) for x in v]
        if isinstance(v, dict): return {k: conv(x) for k, x in v.items()}
        return v
    return conv({"quote_id": value.quote_id, "candidate_id": value.candidate_id, "supplier": value.supplier,
        "model": value.model, "configuration": value.configuration, "amount": value.amount,
        "currency": value.currency, "source_type": value.source_type, "source": value.source,
        "source_location": value.source_location, "quote_date": value.quote_date, "valid_until": value.valid_until,
        "conditions": value.conditions, "note": value.note})
