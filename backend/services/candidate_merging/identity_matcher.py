"""Candidate identity matching with five-layer strategy."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.evidence_validation import normalized_model
from backend.services.text_utils import search_text


_CANDIDATE_KEYS = ("brand", "series", "model", "configuration")


def candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(search_text(candidate.get(key)).replace(" ", "") for key in _CANDIDATE_KEYS)


def compatible_candidate_identity(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_brand, left_series, left_model, left_configuration = left
    right_brand, right_series, right_model, right_configuration = right
    if not left_model or normalized_model(left_model) != normalized_model(right_model):
        return False
    for first, second in (
        (left_brand, right_brand),
        (left_series, right_series),
        (left_configuration, right_configuration),
    ):
        if first and second and first != second:
            return False
    return True
