"""Candidate identity matching and scoring for evidence selection."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.text_utils import search_text, scalar_texts


# Candidate identity keys（与拆分前 deepseek_analysis 的 _CANDIDATE_KEYS 同序；
# candidate_terms 的插入顺序与它绑定）
_CANDIDATE_KEYS = ("brand", "series", "model", "configuration")


def candidate_terms(candidate: Mapping[str, Any]) -> list[tuple[str, str, str, int]]:
    """Return category-neutral identifiers ordered by evidential specificity.

    Returns list of (key, normalized, compact, weight) tuples.
    """
    weights = {"model": 400, "configuration": 240, "series": 180, "brand": 80}
    terms: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    for key in _CANDIDATE_KEYS:
        for raw in scalar_texts(candidate.get(key)):
            normalized = search_text(raw)
            compact = normalized.replace(" ", "")
            if len(compact) < 2:
                continue
            terms[(key, compact)] = (key, normalized, compact, weights[key])
            for token in normalized.split():
                token_compact = token.replace(" ", "")
                # Short generic tokens (for example "96" or "pro") create
                # catalog-wide false hits. Mixed model identifiers remain useful.
                if len(token_compact) >= 4 or (
                    len(token_compact) >= 3
                    and any(char.isalpha() for char in token_compact)
                    and any(char.isdigit() for char in token_compact)
                ):
                    terms.setdefault(
                        (key, token_compact),
                        (key, token, token_compact, max(20, weights[key] // 2)),
                    )
    return sorted(terms.values(), key=lambda item: (-item[3], -len(item[2]), item[0], item[2]))


def matched_candidate_terms(text: str, terms: list[tuple[str, str, str, int]]) -> list[tuple[str, int]]:
    """Find which candidate terms match in the given text.

    Returns list of (key, score) tuples for matched terms.
    """
    normalized = search_text(text)
    compact_text = normalized.replace(" ", "")
    matched: list[tuple[str, int]] = []
    for key, term, compact, score in terms:
        if term in normalized or (any(char.isdigit() for char in compact) and compact in compact_text):
            matched.append((key, score))
    return matched
