"""Character budget allocation for bounded evidence selection."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.text_utils import table_rows_text, text_chunks
from backend.services.evidence_selection.candidate_scorer import matched_candidate_terms
from backend.services.evidence_selection.field_scorer import _field_scores_for_text
from backend.services.evidence_selection.limits import (
    _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_TOTAL_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
)


def bounded_candidate_page_text(
    text: Any,
    terms: list[tuple[str, str, str, int]],
    maximum_characters: int = _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
) -> tuple[str, int, int, int]:
    """Extract the most relevant text window from a page.

    Returns (bounded_text, start_offset, end_offset, original_length).
    """
    value = str(text or "")
    maximum_characters = max(0, min(maximum_characters, _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT))
    if len(value) <= maximum_characters:
        return value, 0, len(value), len(value)
    if maximum_characters == 0:
        return "", 0, 0, len(value)

    chunks = text_chunks(value)
    ranked = sorted(
        chunks,
        key=lambda chunk: (
            -(
                sum(score for _, score in matched_candidate_terms(chunk[2], terms))
                + sum(
                    int(match["score"])
                    for match in _field_scores_for_text(chunk[2]).values()
                )
            ),
            chunk[0],
        ),
    )
    best_start, best_end, _ = ranked[0]
    center = (best_start + best_end) // 2
    start = max(0, center - maximum_characters // 2)
    end = min(len(value), start + maximum_characters)
    start = max(0, end - maximum_characters)
    return value[start:end], start, end, len(value)


def bounded_table_row(row: list[Any] | tuple[Any, ...], maximum_characters: int) -> list[str]:
    """Truncate a table row to fit within character limit."""
    cells: list[str] = []
    used = 0
    for raw_cell in row:
        separator = 1 if cells else 0
        available = maximum_characters - used - separator
        if available <= 0:
            break
        cell = str(raw_cell)
        cells.append(cell[:available])
        used += separator + min(len(cell), available)
        if len(cell) > available:
            break
    return cells


def bounded_candidate_table(
    table: Mapping[str, Any],
    terms: list[tuple[str, str, str, int]],
    maximum_characters: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Select and bound table rows based on relevance and character limit.

    Returns (payload_or_none, stats_dict).
    """
    rows = [list(row) for row in table.get("rows", []) if isinstance(row, (list, tuple))]
    original_text = table_rows_text({"rows": rows})
    original_characters = len(original_text)
    original_row_count = len(rows)
    maximum_characters = max(0, min(maximum_characters, _CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT))

    if not rows or maximum_characters == 0:
        return None, {
            "original_text_characters": original_characters,
            "selected_text_characters": 0,
            "original_row_count": original_row_count,
            "selected_row_count": 0,
            "truncated": bool(original_characters),
        }

    row_scores = {}
    for index, row in enumerate(rows):
        row_text = table_rows_text({"rows": [row]})
        row_scores[index] = (
            sum(score for _, score in matched_candidate_terms(row_text, terms))
            + sum(int(match["score"]) for match in _field_scores_for_text(row_text).values())
        )

    ranked_matches = sorted(
        (index for index, score in row_scores.items() if score > 0),
        key=lambda index: (-row_scores[index], index),
    )
    ranked_indices: list[int] = []
    for index in ([0] if rows else []) + ranked_matches:
        if index not in ranked_indices:
            ranked_indices.append(index)
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(rows) and neighbor not in ranked_indices:
                ranked_indices.append(neighbor)
    ranked_indices.extend(index for index in range(len(rows)) if index not in ranked_indices)

    selected: dict[int, list[str]] = {}
    used = 0
    for index in ranked_indices:
        if len(selected) >= _CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT or used >= maximum_characters:
            break
        separator = 1 if selected else 0
        bounded_row = bounded_table_row(rows[index], maximum_characters - used - separator)
        if not bounded_row:
            continue
        selected[index] = bounded_row
        used += separator + len("\t".join(bounded_row))

    selected_indices = sorted(selected)
    selected_rows = [selected[index] for index in selected_indices]
    selected_characters = len(table_rows_text({"rows": selected_rows}))
    payload = {
        **table,
        "source_location": table.get("source_location") or f"table:{table.get('index', 'unknown')}",
        "rows": selected_rows,
        "selected_row_indices": selected_indices,
        "original_row_count": original_row_count,
        "selected_row_count": len(selected_rows),
        "original_text_characters": original_characters,
        "selected_text_characters": selected_characters,
        "text_truncated": (
            len(selected_rows) < original_row_count or selected_characters < original_characters
        ),
    }
    return payload, {
        "original_text_characters": original_characters,
        "selected_text_characters": selected_characters,
        "original_row_count": original_row_count,
        "selected_row_count": len(selected_rows),
        "truncated": payload["text_truncated"],
    }
