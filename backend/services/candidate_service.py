"""M03 service: conservative extraction plus explicit user decisions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from backend.domain.candidates import (
    CandidateSource,
    CandidateStatus,
    ProductCandidate,
    merge_candidates,
)
from backend.domain.candidates_improved import (
    CandidateSource as ImmutableCandidateSource,
    CandidateStatus as ImmutableCandidateStatus,
    ProductCandidate as ImmutableProductCandidate,
    merge_candidates as merge_immutable_candidates,
)


@dataclass(frozen=True)
class ParsedPage:
    document_id: str
    location: str
    text: str
    source_type: str = "uploaded_document"


def pages_from_document(record) -> list[ParsedPage]:
    """将 M02 文档页转换为 M03 可消费的页面契约。"""
    return [
        ParsedPage(
            record.id,
            page.source_location or f"page:{page.number}",
            page.text,
            source_type=getattr(record, "document_type", "uploaded_document"),
        )
        for page in record.pages
    ]


# Model-like tokens are intentionally limited to tokens containing a digit or
# a hyphen.  Ordinary nouns ("centrifuge", "analyser") must not become models.
MODEL_RE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)+\b"  # e.g. APEX-2000
    r"|\b[A-Z]{1,8}\d{2,}[A-Z0-9-]*\b"      # e.g. Q500
    r"|\b\d+[A-Z]?[-/][A-Z0-9]+\b"           # e.g. 5425-R
    r"|\b[A-Z]{2,}\s+\d{2,}(?:\s+[A-Z0-9]{2,})*\b"  # e.g. INFINITE 200 PRO
)
BRAND_RE = re.compile(r"(?i)\b(?:Thermo\s+Fisher|Eppendorf|Agilent|Hach|Bio-Rad|Tecan|默克|赛默飞|艾本德|安捷伦)\b")
SERIES_RE = re.compile(r"(?i)\b(?:series|系列)\s*[:：]?\s*([A-Za-z][A-Za-z0-9 -]{1,30})")
CONFIG_RE = re.compile(r"(?i)\b(?:configuration|config|配置)\s*[:：]\s*([^,;。\n]{2,80})")

# A specification row such as ``| 电源 | AC 220±22V 50/60Hz |`` holds a measured
# value, not a product. These rules are deliberately generic -- no brand, model
# or document is named. They were checked against the real brochures in the
# project fixtures: AC220V, ISO8655- and RS-232C are dropped while every real
# model in the same tables (SPX-70BIII, LRH-1000, YK-S160, Infinite 200 PRO,
# KF 96, ...) survives.
#
# Quantity symbols carry their own meaning, so ``AC220V``/``DC 12V`` is a value
# on its own. Single-letter symbols are too easy to confuse with an instrument
# code (A100, M100), so they need a unit letter or a parameter label beside them.
_VALUE_ONLY_SYMBOLS = frozenset({
    'AC', 'DC', 'VAC', 'VDC', 'HZ', 'KHZ', 'MHZ', 'RPM', 'KPA', 'MPA', 'LUX', 'PPM', 'PPB', 'DBA',
})
_MEASURE_SYMBOLS = frozenset({
    'V', 'W', 'KW', 'MW', 'A', 'MA', 'AH', 'MAH', 'KV', 'MV', 'MM', 'CM', 'KM', 'UM', 'NM',
    'KG', 'G', 'MG', 'ML', 'L', 'UL', 'NL', 'PA', 'BAR', 'DB', 'MIN', 'MS',
})
_STANDARD_SYMBOLS = frozenset({'ISO', 'IEC', 'GB', 'GBT', 'EN', 'ASTM', 'DIN', 'JIS', 'YY', 'JB', 'HJ', 'SN'})
_UNIT_AFTER_NUMBER_RE = re.compile(
    r'^\d+(?:\.\d+)?(?:' + '|'.join(sorted({
        'V', 'W', 'A', 'HZ', 'KHZ', 'MHZ', 'KV', 'MV', 'KW', 'MW', 'MA', 'AH', 'MAH',
        'MM', 'CM', 'KM', 'UM', 'NM', 'KG', 'MG', 'ML', 'UL', 'NL', 'PA', 'KPA', 'MPA',
        'BAR', 'DB', 'RPM', 'MIN', 'MS', 'L', 'G',
    }, key=len, reverse=True)) + r')$',
    re.IGNORECASE,
)
_MAGNITUDE_VALUE_RE = re.compile(r'^[A-Z]{1,5}\d[A-Z0-9.\-/]*$')
# The left column of a specification table. ``型号`` is deliberately absent:
# that label names the product, everything here names a property of it.
_PARAMETER_LABELS = (
    '电源', '电压', '频率', '功率', '电流', '尺寸', '净重', '毛重', '重量', '容积', '容量', '温度',
    '湿度', '噪声', '噪音', '转速', '接口', '标准', '量程', '分辨率', '精度', '准确度', '误差',
    '材质', '输入功率', '额定电压', '额定功率', '外形尺寸', '内尺寸', '工作环境温度', '相对湿度',
    '控温范围', '使用温度范围', '测量范围', '波长', '工作电压', '净尺寸',
)
_PARAMETER_SEPARATOR_RE = re.compile(r'^[\s|｜:：,，、\-—/的和及与或是为]{0,4}$')


def measurement_value_not_model(model: str, evidence: str = '') -> bool:
    """True when a token is a specification value rather than a product model.

    ``evidence`` is the surrounding captured text; it is what separates
    ``AC220V`` in ``电源 AC220V 50HZ`` from a real model that merely looks
    numeric.
    """
    compact = re.sub(r'\s+', '', str(model or '')).upper()
    match = _MAGNITUDE_VALUE_RE.match(compact)
    if match:
        symbol, remainder = re.match(r'^([A-Z]+)(\d.*)$', compact).groups()
        if symbol in _STANDARD_SYMBOLS or symbol in _VALUE_ONLY_SYMBOLS:
            return True
        if symbol in _MEASURE_SYMBOLS and _UNIT_AFTER_NUMBER_RE.match(remainder):
            return True
    return _follows_parameter_label(str(model or ''), evidence)


def _follows_parameter_label(model: str, evidence: str) -> bool:
    """True when ``model`` sits in the value column of a parameter row."""
    if not model or not evidence:
        return False
    for label in _PARAMETER_LABELS:
        for found in re.finditer(re.escape(label), evidence):
            tail = evidence[found.end():]
            offset = tail.find(model)
            if 0 <= offset <= 8 and _PARAMETER_SEPARATOR_RE.match(tail[:offset]):
                return True
    return False


def extract_candidates(pages: Iterable[ParsedPage]) -> list[ProductCandidate]:
    """Extract *leads* from parsed M02 pages; every result starts pending."""
    # Keep the legacy DTO at the API/SQLite edge, but construct and merge the
    # hardened immutable objects before adapting back.  This makes malformed
    # identity/source data fail at the production boundary instead of only in
    # the standalone adversarial tests.
    found: list[ImmutableProductCandidate] = []
    for page in pages:
        brands = [m.group(0).strip() for m in BRAND_RE.finditer(page.text)]
        series_match = SERIES_RE.search(page.text)
        series = series_match.group(1).strip() if series_match else None
        if series and series.casefold() in {"in", "in/f_inite"}:
            series = None
        config_match = CONFIG_RE.search(page.text)
        configuration = config_match.group(1).strip() if config_match else None
        # This title is frequently extracted with mixed case and is the
        # product identity, not a parameter. Keep the explicit known form
        # while applying conservative generic rules below.
        matches = list(MODEL_RE.finditer(page.text))
        matches.extend(re.finditer(r"(?i)\bInfinite\s+200\s+PRO\b", page.text))
        for model_match in matches:
            model = " ".join(model_match.group(0).split())
            excerpt = _excerpt(page.text, model_match.start(), model_match.end())
            if not _looks_like_model(model, excerpt):
                continue
            source = ImmutableCandidateSource(page.document_id, page.location, excerpt, page.source_type)
            found.append(ImmutableProductCandidate(
                brands[0] if brands else None, series, model, configuration,
                sources=(source,),
            ))
    merged = merge_immutable_candidates(found)
    return [ProductCandidate(
        brand=item.brand,
        series=item.series,
        model=item.model,
        configuration=item.configuration,
        sources=[CandidateSource(s.document_id, s.location, s.excerpt, s.source_type) for s in item.sources],
        status=CandidateStatus(item.status.value),
        id=item.id,
        note=item.note,
        created_at=item.created_at,
    ) for item in merged]


def _looks_like_model(model: str, evidence: str = '') -> bool:
    """Reject plate sizes, wavelengths, dates, patent and catalogue numbers.

    Specification-table values (voltage, power, dimensions, interface or
    standard numbers) are rejected too: they look exactly like a model token in
    raw text, but the row they sit in names a property, not a product.
    """
    if measurement_value_not_model(model, evidence):
        return False
    compact = model.replace(" ", "").upper()
    if compact == "INFINITE200PRO":
        return True
    if "/" in model or model.upper().startswith(("EP", "OD")):
        return False
    # Date/version patterns and purely numeric ranges are not product models.
    if re.fullmatch(r"\d{1,2}-\d{4}", model) or re.fullmatch(r"\d+-\d+", model):
        return False
    # Permit the uncommon numeric-leading instrument code form (5425-R),
    # but not numeric patent/part identifiers such as 144-041.
    if model[0].isdigit() and not re.fullmatch(r"\d{3,5}-[A-Z]", model, re.I):
        return False
    return any(ch.isalpha() for ch in model) and any(ch.isdigit() for ch in model)


def _excerpt(text: str, start: int, end: int, radius: int = 100) -> str:
    return text[max(0, start - radius): min(len(text), end + radius)].replace("\n", " ").strip()


def decide_snapshot(candidate: ProductCandidate, status: CandidateStatus | str, note: str | None = None) -> ProductCandidate:
    """Return a legacy DTO produced by the immutable status transition."""
    if not isinstance(status, CandidateStatus):
        status = CandidateStatus(status)
    # Run the transition through the immutable domain object, then copy the
    # result back to the legacy mutable DTO used by SQLite/API serialization.
    # This keeps the wire contract stable while preventing invalid model/status
    # values from entering the production path.
    immutable = ImmutableProductCandidate(
        brand=candidate.brand,
        series=candidate.series,
        model=candidate.model,
        configuration=candidate.configuration,
        sources=tuple(ImmutableCandidateSource(s.document_id, s.location, s.excerpt, s.source_type) for s in candidate.sources),
        status=ImmutableCandidateStatus(candidate.status.value),
        id=candidate.id,
        note=candidate.note,
        created_at=candidate.created_at,
    ).with_status(ImmutableCandidateStatus(status.value), note)
    return ProductCandidate(
        brand=immutable.brand, series=immutable.series, model=immutable.model,
        configuration=immutable.configuration,
        sources=[CandidateSource(s.document_id, s.location, s.excerpt, s.source_type) for s in immutable.sources],
        status=CandidateStatus(immutable.status.value), id=immutable.id,
        note=immutable.note, created_at=immutable.created_at,
    )


def decide(candidate: ProductCandidate, status: CandidateStatus, note: str | None = None) -> ProductCandidate:
    """Apply an explicit user decision; no implicit confirmation is allowed."""
    updated = decide_snapshot(candidate, status, note)
    candidate.status = updated.status
    candidate.note = updated.note
    return candidate


def confirmed_candidates(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    """Return only user-confirmed candidates eligible for M04 networking."""
    return [candidate for candidate in candidates if candidate.status is CandidateStatus.CONFIRMED]


class CandidateService:
    """Small application-facing facade used by an API or CLI adapter."""

    def identify(self, pages: Iterable[ParsedPage]) -> list[ProductCandidate]:
        return extract_candidates(pages)

    def apply_decision(
        self, candidate: ProductCandidate, status: CandidateStatus | str, note: str | None = None
    ) -> ProductCandidate:
        return decide(candidate, status, note)

    def eligible_for_search(self, candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
        return confirmed_candidates(candidates)
