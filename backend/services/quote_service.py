"""Deterministic M09 quote comparison service."""
from __future__ import annotations
from collections import defaultdict
from itertools import combinations
from backend.domain.errors import ModuleValidationError
from backend.domain.quotes import SupplierQuote, QuoteSourceType, PriceComparison, ProcurementComparison

CURRENT = {QuoteSourceType.CURRENT_SUPPLIER_QUOTE, QuoteSourceType.OFFICIAL_PUBLIC_PRICE}

def deduplicate_quotes(quotes):
    seen = {}
    for q in quotes:
        if not isinstance(q, SupplierQuote):
            raise ModuleValidationError("quotes must contain SupplierQuote", code="invalid_quote")
        seen.setdefault(q.quote_id, q)
    return tuple(seen[k] for k in sorted(seen))

def compare_quotes(quotes, *, candidate_id=None) -> ProcurementComparison:
    qs = deduplicate_quotes(quotes)
    if candidate_id is not None:
        qs = tuple(q for q in qs if q.candidate_id == candidate_id)
    if not qs:
        raise ModuleValidationError("at least one quote is required", code="empty_quotes")
    comps, incomps, gaps = [], [], set()
    for a, b in combinations(qs, 2):
        reasons = []
        if a.configuration_key != b.configuration_key: reasons.append("configuration_mismatch")
        if a.currency != b.currency: reasons.append("currency_mismatch")
        at = next((c.value for c in a.conditions if c.condition_type.value == "tax"), "unknown")
        bt = next((c.value for c in b.conditions if c.condition_type.value == "tax"), "unknown")
        if at != bt: reasons.append("tax_condition_mismatch")
        if at == "unknown" or bt == "unknown": reasons.append("tax_condition_unknown")
        if a.source_type not in CURRENT or b.source_type not in CURRENT: reasons.append("historical_or_lead_reference_only")
        missing = []
        for q in (a, b):
            kinds = {c.condition_type.value for c in q.conditions}
            missing.extend(f"{k}:unknown" for k in ("tax", "delivery", "installation", "warranty", "after_sales", "payment") if k not in kinds)
        pc = PriceComparison((a.quote_id, b.quote_id), not reasons, "comparable" if not reasons else ";".join(reasons),
                             b.amount-a.amount if not reasons else None, tuple(sorted(set(missing))))
        (comps if pc.comparable else incomps).append(pc); gaps.update(pc.gaps)
    return ProcurementComparison(qs[0].candidate_id if candidate_id is None else candidate_id, qs, tuple(comps), tuple(incomps), tuple(sorted(gaps)))

def aggregate_quotes(quotes, *, by="candidate_id"):
    if by not in {"candidate_id", "supplier"}: raise ModuleValidationError("by must be candidate_id or supplier", code="invalid_group")
    groups = defaultdict(list)
    for q in deduplicate_quotes(quotes): groups[getattr(q, by)].append(q)
    return {key: compare_quotes(value, candidate_id=key if by == "candidate_id" else None) for key, value in sorted(groups.items())}
