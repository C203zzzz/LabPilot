"""Excerpt validation against source evidence."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Mapping

from backend.services.evidence_validation.text_normalization import (
    search_text,
    scalar_texts,
)
from backend.services.evidence_validation.numeric_matching import numeric_tokens


_REFERENCE_FILLER_TOKENS = frozenset({
    "a", "an", "at", "from", "in", "located", "of", "on", "source", "the",
})
_REFERENCE_LOCATION_SUFFIX_RE = re.compile(
    r"(?:[\s,;，；\[(（]*)"
    r"(?:(?:in|at|on|from)\s+)?(?:page|p\.?)\s*[:#]?\s*\d+"
    r"(?:\s*/\s*rendered)?[\s\])）]*$",
    re.IGNORECASE,
)
_REFERENCE_CHINESE_LOCATION_SUFFIX_RE = re.compile(
    r"(?:[\s,;，；\[(（]*)(?:见|位于|来自)?第?\s*\d+\s*页[\s\])）]*$",
)


def _source(raw: Any) -> dict[str, Any]:
    """Normalize a source reference to standard form."""
    if not isinstance(raw, Mapping):
        return {"document_id": None, "page_or_location": None, "excerpt": ""}
    return {
        "document_id": raw.get("document_id"),
        "page_or_location": raw.get("page_or_location", raw.get("location", raw.get("page"))),
        "excerpt": str(raw.get("excerpt", ""))[:2000],
    }


def _table_rows_text(table: Mapping[str, Any]) -> str:
    """Convert table rows to plain text."""
    return "\n".join(
        "\t".join(str(cell) for cell in row)
        for row in table.get("rows", []) if isinstance(row, (list, tuple))
    )


def _is_local_ordered_subsequence(needles: list[str], haystack: list[str]) -> bool:
    """Check if needles appear in order within a tight span in haystack."""
    if not needles:
        return False
    position = 0
    first_match = None
    last_match = None
    for index, token in enumerate(haystack):
        if token == needles[position]:
            first_match = index if first_match is None else first_match
            last_match = index
            position += 1
            if position == len(needles):
                span = int(last_match) - int(first_match) + 1
                return span <= len(needles) + max(2, len(needles) // 2)
    return False


def excerpt_matches_evidence(excerpt: Any, evidence: str) -> bool:
    """Verify that excerpt text appears verbatim in evidence.

    Applies multiple normalization strategies and permits tight ordered
    subsequences after removing location suffixes.
    """
    excerpt_text = str(excerpt or "").strip()
    if not excerpt_text or not evidence.strip():
        return False

    # Remove location suffixes like "page:1" or "见第1页"
    excerpt_core = _REFERENCE_LOCATION_SUFFIX_RE.sub("", excerpt_text).strip()
    excerpt_core = _REFERENCE_CHINESE_LOCATION_SUFFIX_RE.sub("", excerpt_core).strip()

    normalized_excerpt = search_text(excerpt_core)
    normalized_evidence = search_text(evidence)

    # Try compact match (no spaces)
    compact_excerpt = normalized_excerpt.replace(" ", "")
    compact_evidence = normalized_evidence.replace(" ", "")
    if compact_excerpt and compact_excerpt in compact_evidence:
        return True

    # Try clause-by-clause match for semicolon/newline-separated excerpts
    clauses = [
        search_text(clause).replace(" ", "")
        for clause in re.split(r"[;；\n]+", excerpt_core)
        if search_text(clause).replace(" ", "")
    ]
    if len(clauses) > 1 and all(clause in compact_evidence for clause in clauses):
        return True

    # Permit tight ordered token subsequence
    excerpt_tokens = [
        token for token in normalized_excerpt.split()
        if token not in _REFERENCE_FILLER_TOKENS
    ]
    return len(excerpt_tokens) >= 2 and _is_local_ordered_subsequence(
        excerpt_tokens, normalized_evidence.split(),
    )


def validated_sources(
    raw_sources: Any,
    evidence_by_location: Mapping[tuple[str, str], str],
    *,
    required_value: Any = None,
) -> list[dict[str, Any]]:
    """Validate sources by checking excerpts against evidence.

    Optionally requires all numeric tokens from required_value to appear
    in the validated sources' excerpts.
    """
    validated = []
    for raw in raw_sources if isinstance(raw_sources, list) else []:
        source = _source(raw)
        key = (
            str(source.get("document_id") or ""),
            str(source.get("page_or_location") or ""),
        )
        evidence = evidence_by_location.get(key)
        if evidence is not None and excerpt_matches_evidence(source.get("excerpt"), evidence):
            validated.append(source)

    # Check numeric coverage if required_value is provided
    required_numbers = numeric_tokens(required_value)
    if required_numbers:
        cited_numbers: set[str] = set()
        for source in validated:
            cited_numbers.update(numeric_tokens(source.get("excerpt")))
        if not required_numbers.issubset(cited_numbers):
            return []

    return validated


def source_evidence_index(documents: list[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """Index exactly the page/table evidence made available to a model call.

    Returns a mapping from (document_id, location) to full text evidence.
    """
    indexed: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for doc in documents:
        document_id = str(doc.get("document_id", doc.get("id")) or "")
        if not document_id:
            continue

        # Index page text
        for page in doc.get("pages", []) if isinstance(doc.get("pages", []), list) else []:
            if not isinstance(page, Mapping):
                continue
            location = page.get("location", page.get("source_location"))
            if location:
                evidence_parts = [str(page.get("text", ""))]
                visual_fields = page.get("visual_fields")
                if isinstance(visual_fields, Mapping):
                    evidence_parts.extend(scalar_texts(visual_fields))
                indexed[(document_id, str(location))].append("\n".join(evidence_parts))

        # Index table text
        for table in doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []:
            if not isinstance(table, Mapping):
                continue
            location = table.get("source_location") or f"table:{table.get('index', 'unknown')}"
            indexed[(document_id, str(location))].append(_table_rows_text(table))

    return {key: "\n".join(parts) for key, parts in indexed.items()}
