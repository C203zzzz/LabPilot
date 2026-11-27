"""Numeric token extraction and canonical number formatting."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.services.evidence_validation.text_normalization import scalar_texts


_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")


def canonical_number(raw: str) -> str:
    """Convert number string to canonical form for comparison.

    Handles comma/decimal disambiguation and normalizes Decimal representation.
    """
    value = raw
    # Disambiguate comma: if only commas (no dots), check if it's a thousands separator
    if "," in value and "." not in value:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", value):
            # Thousands separator: 1,234 -> 1234
            value = value.replace(",", "")
        else:
            # European decimal: 1,5 -> 1.5
            value = value.replace(",", ".")
    else:
        # Has dot or no separators, remove commas
        value = value.replace(",", "")

    try:
        number = Decimal(value)
    except InvalidOperation:
        return value.casefold()

    if not number.is_finite():
        return value.casefold()

    # Format and remove trailing zeros
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def numeric_tokens(value: Any) -> set[str]:
    """Extract all numeric tokens from value in canonical form.

    Returns a set of normalized number strings for comparison.
    """
    tokens: set[str] = set()
    for scalar in scalar_texts(value):
        normalized = unicodedata.normalize("NFKC", scalar)
        tokens.update(
            canonical_number(match.group(0))
            for match in _NUMERIC_TOKEN_RE.finditer(normalized)
        )
    return tokens
