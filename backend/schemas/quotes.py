"""JSON helpers for M09."""
from typing import Any
from backend.domain.quotes import SupplierQuote, ProcurementComparison

def supplier_quote_to_dict(value: SupplierQuote) -> dict[str, Any]: return value.to_dict()
def procurement_comparison_to_dict(value: ProcurementComparison) -> dict[str, Any]: return value.to_dict()

