"""Budget allocation and document assembly for evidence selection."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from backend.services.evidence_selection.budget_allocator import (
    bounded_candidate_page_text as _bounded_candidate_page_text,
    bounded_candidate_table as _bounded_candidate_table,
)
from backend.services.evidence_selection.candidate_scorer import (
    candidate_terms as _candidate_terms,
)

from backend.services.evidence_selection.limits import (
    _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT,
    _CANDIDATE_EVIDENCE_TABLE_TOTAL_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
)


def allocate_table_budget(
    table_candidates: list[dict[str, Any]],
    selected_table_keys: set[tuple[int, int]],
    inventory_source_locations: set[tuple[str, str]],
    prompt_docs: list[dict[str, Any]],
    terms: list[tuple[str, str, str, int]],
) -> tuple[
    dict[int, list[dict[str, Any]]],  # bounded_tables_by_doc
    set[tuple[int, int]],  # selected_table_keys (updated)
    int,  # selected_table_text_char_count
    int,  # truncated_table_count
    list[dict[str, Any]],  # selected_table_locations
]:
    """分配表格预算并选择相关表格行。

    返回:
        (bounded_tables_by_doc, selected_table_keys, selected_table_text_char_count,
         truncated_table_count, selected_table_locations)
    """
    bounded_tables_by_doc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    selected_table_keys = set()
    selected_table_text_char_count = 0
    truncated_table_count = 0
    selected_table_locations: list[dict[str, Any]] = []

    remaining_table_budget = _CANDIDATE_EVIDENCE_TABLE_TOTAL_CHAR_LIMIT
    for entry in table_candidates:
        if remaining_table_budget <= 0:
            break
        table = entry["table"]
        table_payload, stats = _bounded_candidate_table(
            table, terms, remaining_table_budget
        )
        if not table_payload:
            continue
        doc_index = entry["doc_index"]
        bounded_tables_by_doc[doc_index].append(table_payload)
        selected_table_keys.add(entry["key"])
        selected_table_text_char_count += stats["selected_text_characters"]
        remaining_table_budget -= stats["selected_text_characters"]
        if stats["truncated"]:
            truncated_table_count += 1
        doc = prompt_docs[doc_index]
        selected_table_locations.append({
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "location": table_payload.get("source_location"),
            "retrieval_score": entry["profile"]["score"] + entry["candidate_score"],
            "candidate_match_score": entry["candidate_score"],
            "field_scores": entry["profile"]["field_scores"],
            "matched_terms": entry["profile"]["matched_terms"],
            "retrieval_reasons": sorted(
                (["inventory_source"] if (
                    str(doc.get("document_id") or ""), str(entry["location"])
                ) in inventory_source_locations else [])
                + (["candidate_match"] if entry["candidate_score"] else [])
                + (["canonical_field_match"] if entry["profile"]["score"] else [])
            ),
            "top_scored_chunks": entry["profile"]["chunks"][:3],
            **stats,
        })

    return (
        bounded_tables_by_doc,
        selected_table_keys,
        selected_table_text_char_count,
        truncated_table_count,
        selected_table_locations,
    )


def allocate_page_budget(
    all_keys: list[tuple[int, int]],
    selected: set[tuple[int, int]],
    page_by_key: dict[tuple[int, int], dict[str, Any]],
    page_retrieval_profiles: dict[tuple[int, int], dict[str, Any]],
    scores: dict[tuple[int, int], int],
    reasons: dict[tuple[int, int], set[str]],
    prompt_docs: list[dict[str, Any]],
    terms: list[tuple[str, str, str, int]],
    selected_table_text_char_count: int,
) -> tuple[
    dict[int, list[dict[str, Any]]],  # selected_page_payloads_by_doc
    int,  # selected_page_text_char_count
    int,  # selected_original_page_text_char_count
    int,  # truncated_page_count
    list[dict[str, Any]],  # selected_locations
]:
    """分配页面预算并选择相关页面文本。

    返回:
        (selected_page_payloads_by_doc, selected_page_text_char_count,
         selected_original_page_text_char_count, truncated_page_count, selected_locations)
    """
    selected_page_payloads_by_doc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    selected_page_text_char_count = 0
    selected_original_page_text_char_count = 0
    truncated_page_count = 0
    selected_locations: list[dict[str, Any]] = []

    remaining_page_budget = _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT - selected_table_text_char_count
    ordered_selected_keys = [key for key in all_keys if key in selected]
    for position, key in enumerate(ordered_selected_keys):
        pages_left = len(ordered_selected_keys) - position
        fair_share = remaining_page_budget // pages_left if pages_left else 0
        doc_index, _ = key
        doc = prompt_docs[doc_index]
        page = page_by_key[key]
        bounded_text, text_start, text_end, original_text_length = _bounded_candidate_page_text(
            page.get("text", ""), terms,
            min(_CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT, fair_share),
        )
        selected_page_text_char_count += len(bounded_text)
        selected_original_page_text_char_count += original_text_length
        remaining_page_budget -= len(bounded_text)
        if original_text_length > len(bounded_text):
            truncated_page_count += 1
        selected_page_payloads_by_doc[doc_index].append({
            **page,
            "text": bounded_text,
            "text_truncated": original_text_length > len(bounded_text),
            "text_char_start": text_start,
            "text_char_end": text_end,
            "original_text_characters": original_text_length,
            "provenance": {
                "document_id": doc.get("document_id"),
                "source_name": doc.get("source_name"),
                "analysis_input": page.get("analysis_input", doc.get("analysis_input")),
                "page": page.get("page"),
                "location": page.get("location"),
            },
        })

    # 构建selected_locations
    for key in ordered_selected_keys:
        doc_index, _ = key
        doc = prompt_docs[doc_index]
        page = page_by_key[key]
        retrieval_profile = page_retrieval_profiles.get(key, {
            "score": 0, "field_scores": {}, "matched_terms": {}, "chunks": [],
        })
        selected_locations.append({
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "analysis_input": page.get("analysis_input", doc.get("analysis_input")),
            "page": page.get("page"),
            "location": page.get("location"),
            "selection_reasons": sorted(reasons[key]),
            "retrieval_score": retrieval_profile["score"] + scores[key],
            "candidate_match_score": scores[key],
            "field_scores": retrieval_profile.get("page_field_scores", {}),
            "matched_terms": retrieval_profile.get("page_matched_terms", {}),
            "selection_field_scores": retrieval_profile["field_scores"],
            "selection_matched_terms": retrieval_profile["matched_terms"],
            "top_scored_chunks": retrieval_profile["chunks"][:3],
        })

    return (
        selected_page_payloads_by_doc,
        selected_page_text_char_count,
        selected_original_page_text_char_count,
        truncated_page_count,
        selected_locations,
    )


def assemble_documents(
    prompt_docs: list[dict[str, Any]],
    selected_doc_keys_by_index: dict[int, set[tuple[int, int]]],
    bounded_tables_by_doc: dict[int, list[dict[str, Any]]],
    selected_page_payloads_by_doc: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """组装最终的文档列表，包含选中的页面和表格。

    返回:
        selected_documents: 包含选中页面和表格的文档列表
    """
    selected_documents: list[dict[str, Any]] = []
    for doc_index, doc in enumerate(prompt_docs):
        selected_doc_keys = selected_doc_keys_by_index[doc_index]
        if not selected_doc_keys:
            continue
        selected_tables = bounded_tables_by_doc[doc_index]
        selected_page_payloads = selected_page_payloads_by_doc[doc_index]
        selected_documents.append({
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "analysis_input": doc.get("analysis_input"),
            "provenance": {
                "document_id": doc.get("document_id"),
                "source_name": doc.get("source_name"),
                "analysis_input": doc.get("analysis_input"),
            },
            "pages": selected_page_payloads,
            "tables": selected_tables,
        })

    return selected_documents
