"""Audit information building for evidence selection results."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.evidence_selection.limits import (
    _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
    _CANDIDATE_EVIDENCE_PAGE_LIMIT,
    _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
    _FIELD_RETRIEVAL_TOP_K,
)
from backend.services.evidence_selection.page_selector import CANONICAL_FIELD_NAMES


def build_audit_info(
    all_keys: list[tuple[int, int]],
    selected: set[tuple[int, int]],
    page_retrieval_profiles: dict[tuple[int, int], dict[str, Any]],
    prompt_docs: list[Mapping[str, Any]],
    page_by_key: dict[tuple[int, int], Mapping[str, Any]],
    table_entries: list[dict[str, Any]],
    selected_table_keys: set[tuple[int, int]],
    selected_before_fallback: int,
    fallback_reason: str | None,
    unresolved_sources: list[dict[str, Any]],
    selected_page_text_char_count: int,
    selected_original_page_text_char_count: int,
    selected_table_text_char_count: int,
    selected_original_table_text_char_count: int,
    truncated_page_count: int,
    truncated_table_count: int,
    selected_locations: list[dict[str, Any]],
    selected_table_locations: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    构建完整的审计信息，包括字段覆盖度统计和检索单元分析。

    参数:
        all_keys: 所有页面key列表
        selected: 选中的页面key集合
        page_retrieval_profiles: 页面检索profile
        table_entries: 表格条目列表
        selected_table_keys: 选中的表格key集合
        selected_before_fallback: fallback前的选中数量
        fallback_reason: fallback原因
        unresolved_sources: 未解析的来源
        selected_page_text_char_count: 选中页面文本字符数
        selected_original_page_text_char_count: 原始页面文本字符数
        selected_table_text_char_count: 选中表格文本字符数
        selected_original_table_text_char_count: 原始表格文本字符数
        truncated_page_count: 截断的页面数
        truncated_table_count: 截断的表格数
        selected_locations: 选中的页面位置信息
        selected_table_locations: 选中的表格位置信息

    返回:
        包含evidence_selection统计信息的字典
    """
    # 构建检索单元（用于字段覆盖度分析）
    retrieval_units: list[dict[str, Any]] = []

    # 添加页面单元（document_id / location 取实值，审计要能定位到具体某一页）
    for key, profile in page_retrieval_profiles.items():
        doc_index, _ = key
        doc = prompt_docs[doc_index]
        page = page_by_key[key]
        retrieval_units.append({
            "kind": "page",
            "key": key,
            "document_id": doc.get("document_id"),
            "location": page.get("location"),
            "score": profile.get("page_score", 0),
            "field_scores": profile.get("page_field_scores", {}),
            "matched_terms": profile.get("page_matched_terms", {}),
            "selected": key in selected,
        })

    # 添加表格单元
    for entry in table_entries:
        doc = prompt_docs[entry["doc_index"]]
        retrieval_units.append({
            "kind": "table",
            "key": entry["key"],
            "document_id": doc.get("document_id"),
            "location": entry["location"],
            "score": entry["profile"]["score"],
            "field_scores": entry["profile"]["field_scores"],
            "matched_terms": entry["profile"]["matched_terms"],
            "selected": entry["key"] in selected_table_keys,
        })

    # 计算字段覆盖度
    field_coverage = {}
    for field_name in CANONICAL_FIELD_NAMES:
        ranked_units = sorted(
            (
                unit for unit in retrieval_units
                if unit["field_scores"].get(field_name, 0) > 0
            ),
            key=lambda unit: (
                -unit["field_scores"][field_name],
                -unit["score"],
                unit["kind"],
                str(unit["document_id"]),
                str(unit["location"]),
            ),
        )
        selected_units = [unit for unit in ranked_units if unit["selected"]]

        def audit_unit(unit: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "kind": unit["kind"],
                "document_id": unit["document_id"],
                "location": unit["location"],
                "score": unit["field_scores"][field_name],
                "matched_terms": unit["matched_terms"].get(field_name, []),
                "selected": unit["selected"],
            }

        field_coverage[field_name] = {
            "covered": bool(selected_units),
            "selected_evidence_count": len(selected_units),
            "top_k": [audit_unit(unit) for unit in ranked_units[:_FIELD_RETRIEVAL_TOP_K]],
            "selected_evidence": [audit_unit(unit) for unit in selected_units],
        }

    covered_canonical_fields = [
        field_name for field_name in CANONICAL_FIELD_NAMES
        if field_coverage[field_name]["covered"]
    ]
    uncovered_canonical_fields = [
        field_name for field_name in CANONICAL_FIELD_NAMES
        if not field_coverage[field_name]["covered"]
    ]

    omitted = max(0, len(all_keys) - len(selected))
    selected_text_char_count = selected_page_text_char_count + selected_table_text_char_count
    selected_original_text_char_count = (
        selected_original_page_text_char_count + selected_original_table_text_char_count
    )

    return {
        "scope": "candidate_bounded_page_subset",
        "page_limit": _CANDIDATE_EVIDENCE_PAGE_LIMIT,
        "page_text_character_limit": _CANDIDATE_EVIDENCE_PAGE_CHAR_LIMIT,
        "text_character_budget": _CANDIDATE_EVIDENCE_TEXT_CHAR_LIMIT,
        "selected_text_character_count": selected_text_char_count,
        "selected_original_text_character_count": selected_original_text_char_count,
        "omitted_selected_page_text_characters": (
            selected_original_page_text_char_count - selected_page_text_char_count
        ),
        "selected_page_text_character_count": selected_page_text_char_count,
        "selected_original_page_text_character_count": selected_original_page_text_char_count,
        "selected_table_text_character_count": selected_table_text_char_count,
        "selected_original_table_text_character_count": selected_original_table_text_char_count,
        "omitted_selected_table_text_characters": (
            selected_original_table_text_char_count - selected_table_text_char_count
        ),
        "omitted_selected_text_characters": (
            selected_original_text_char_count - selected_text_char_count
        ),
        "truncated_selected_page_count": truncated_page_count,
        "candidate_table_count": len([e for e in table_entries]),  # 实际数值在调用时传入
        "selected_table_count": len(selected_table_locations),
        "truncated_selected_table_count": truncated_table_count,
        "total_page_count": len(all_keys),
        "selected_page_count": len(selected),
        "omitted_page_count": omitted,
        "fallback_used": len(selected) > selected_before_fallback,
        "fallback_reason": fallback_reason,
        "unresolved_inventory_sources": unresolved_sources,
        "retrieval_strategy": "canonical_field_top_k_with_identity_anchors",
        "retrieval_top_k_per_field": _FIELD_RETRIEVAL_TOP_K,
        "covered_canonical_fields": covered_canonical_fields,
        "uncovered_canonical_fields": uncovered_canonical_fields,
        "field_coverage": field_coverage,
        "selected_pages": selected_locations,
        "selected_tables": selected_table_locations,
        "notice": (
            f"Only {len(selected)} of {len(all_keys)} source pages are supplied for this candidate; "
            f"{omitted} pages, "
            f"{selected_original_page_text_char_count - selected_page_text_char_count} page characters, "
            f"and {selected_original_table_text_char_count - selected_table_text_char_count} table "
            "characters are omitted. Missing facts must remain unknown."
        ),
    }
