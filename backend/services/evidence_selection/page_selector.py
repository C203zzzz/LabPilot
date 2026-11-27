"""Page selection logic for candidate evidence."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.evidence_selection.evidence_collector import (
    _page_number,
)

# 规范字段名（与deepseek_analysis.py保持同步）
CANONICAL_FIELD_NAMES = (
    "product_identity_and_configuration",
    "core_function_and_applications",
    "detection_or_processing_modes",
    "capacity_and_throughput",
    "sample_and_format_compatibility",
    "performance_range",
    "accuracy_sensitivity_and_repeatability",
    "temperature_environment_and_safety",
    "software_data_and_compliance",
    "accessories_consumables_and_options",
    "physical_dimensions_power_and_installation",
    "price_warranty_delivery_and_service",
)

# 证据选择常量（预算类常量集中在 limits，见其模块说明）
from backend.services.evidence_selection.limits import (
    _CANDIDATE_EVIDENCE_PAGE_LIMIT,
    _FIELD_RETRIEVAL_TOP_K,
)

_CANDIDATE_IDENTITY_SOURCE_PAGE_LIMIT = 2
_CANDIDATE_EVIDENCE_FALLBACK_PAGES = 3


def select_pages(
    candidate: Mapping[str, Any],
    prompt_docs: list[dict[str, Any]],
    evidence_units: dict[str, Any],
) -> dict[str, Any]:
    """
    选择最相关的页面子集用于候选品详情分析。

    参数:
        candidate: 候选品数据
        prompt_docs: 提示文档列表
        evidence_units: 从collect_evidence_units返回的证据单元

    返回:
        包含以下键的字典:
        - selected: 选中的页面key集合
        - reasons: 更新后的选择理由
        - field_page_rankings: 每个字段的页面排名
        - unresolved_sources: 未解析的来源
        - source_keys: 来源页面key集合
        - selected_before_fallback: fallback前的选中数量
        - fallback_reason: fallback原因（如果使用）
    """
    all_keys = evidence_units["all_keys"]
    page_by_key = evidence_units["page_by_key"]
    page_keys_by_doc = evidence_units["page_keys_by_doc"]
    page_lookup = evidence_units["page_lookup"]
    reasons = evidence_units["reasons"]
    scores = evidence_units["scores"]
    page_retrieval_profiles = evidence_units["page_retrieval_profiles"]

    # 解析候选品来源
    unresolved_sources: list[dict[str, Any]] = []
    source_keys: set[tuple[int, int]] = set()
    raw_sources = candidate.get("sources", [])

    for source in raw_sources if isinstance(raw_sources, list) else []:
        if not isinstance(source, Mapping):
            continue
        source_document_id = source.get("document_id")
        source_location = source.get("page_or_location", source.get("location", source.get("page")))
        source_page = _page_number(source_location)
        matching_docs = [
            index for index, doc in enumerate(prompt_docs)
            if source_document_id is not None and doc.get("document_id") == source_document_id
        ]
        if source_document_id is None and len(prompt_docs) == 1:
            matching_docs = [0]
        matching_keys: list[tuple[int, int]] = []
        if source_page is not None:
            for doc_index in matching_docs:
                matching_keys.extend(page_lookup.get((doc_index, source_page), []))
        if not matching_keys:
            unresolved_sources.append({
                "document_id": source_document_id,
                "page_or_location": source_location,
            })
            continue
        for key in matching_keys:
            source_keys.add(key)
            reasons[key].add("inventory_source")

    # 开始页面选择
    selected: set[tuple[int, int]] = set()

    def add_key(key: tuple[int, int], reason: str | None = None) -> None:
        """添加页面到选择集合"""
        if len(selected) >= _CANDIDATE_EVIDENCE_PAGE_LIMIT:
            return
        selected.add(key)
        if reason:
            reasons[key].add(reason)

    def add_neighbors(key: tuple[int, int]) -> None:
        """添加相邻页面"""
        doc_index, _ = key
        doc_keys = page_keys_by_doc.get(doc_index, [])
        try:
            position = doc_keys.index(key)
        except ValueError:
            return
        for neighbor_position in (position - 1, position + 1):
            if 0 <= neighbor_position < len(doc_keys):
                add_key(doc_keys[neighbor_position], "adjacent_context")

    # 1. 添加排名靠前的来源页面
    ranked_sources = sorted(
        source_keys,
        key=lambda key: (-scores[key], key[0], key[1]),
    )[:_CANDIDATE_IDENTITY_SOURCE_PAGE_LIMIT]
    for key in ranked_sources:
        add_key(key)
    for key in ranked_sources:
        add_neighbors(key)

    # 2. 基于字段的Top-K选择
    field_page_rankings: dict[str, list[tuple[int, int]]] = {}
    field_top_k_pool: set[tuple[int, int]] = set()
    for field_name in CANONICAL_FIELD_NAMES:
        ranked = sorted(
            (
                key for key, profile in page_retrieval_profiles.items()
                if profile["field_scores"].get(field_name, 0) > 0
            ),
            key=lambda key: (
                -page_retrieval_profiles[key]["field_scores"][field_name],
                -page_retrieval_profiles[key]["score"],
                key[0], key[1],
            ),
        )[:_FIELD_RETRIEVAL_TOP_K]
        field_page_rankings[field_name] = ranked
        field_top_k_pool.update(ranked)

    # 3. 选择覆盖最多新字段的页面
    covered_fields = {
        field_name
        for key in selected
        for field_name in page_retrieval_profiles.get(key, {}).get("field_scores", {})
    }
    field_selected_keys: list[tuple[int, int]] = []
    while field_top_k_pool and len(selected) < _CANDIDATE_EVIDENCE_PAGE_LIMIT:
        remaining = [key for key in field_top_k_pool if key not in selected]
        if not remaining:
            break
        best = max(
            remaining,
            key=lambda key: (
                len(set(page_retrieval_profiles[key]["field_scores"]) - covered_fields),
                sum(
                    score for field_name, score in page_retrieval_profiles[key]["field_scores"].items()
                    if field_name not in covered_fields
                ),
                page_retrieval_profiles[key]["score"],
                scores[key],
                -key[0], -key[1],
            ),
        )
        new_fields = set(page_retrieval_profiles[best]["field_scores"]) - covered_fields
        if not new_fields:
            break
        add_key(best, "canonical_field_top_k")
        field_selected_keys.append(best)
        for field_name in page_retrieval_profiles[best]["field_scores"]:
            if best in field_page_rankings[field_name]:
                reasons[best].add(f"field_top_k:{field_name}")
        covered_fields.update(page_retrieval_profiles[best]["field_scores"])
        field_top_k_pool.discard(best)

    # 4. 使用剩余的页面槽位选择最强的二次证据
    remaining_field_pages = sorted(
        (key for key in field_top_k_pool if key not in selected),
        key=lambda key: (
            -page_retrieval_profiles[key]["score"],
            -scores[key],
            key[0], key[1],
        ),
    )
    for key in remaining_field_pages:
        if len(selected) >= _CANDIDATE_EVIDENCE_PAGE_LIMIT:
            break
        add_key(key, "canonical_field_top_k")
        field_selected_keys.append(key)
        for field_name in page_retrieval_profiles[key]["field_scores"]:
            if key in field_page_rankings[field_name]:
                reasons[key].add(f"field_top_k:{field_name}")

    # 5. 添加候选品匹配页面
    ranked_matches = sorted(
        (key for key, score in scores.items() if score > 0 and key not in source_keys),
        key=lambda key: (-scores[key], key[0], key[1]),
    )
    for key in ranked_matches:
        add_key(key)
    # 只扩展模型身份匹配的相邻页面
    for key in ranked_matches:
        if key in selected:
            add_neighbors(key)

    # 6. Fallback：如果选中页面太少，添加代表性页面
    selected_before_fallback = len(selected)
    fallback_target = min(len(all_keys), _CANDIDATE_EVIDENCE_FALLBACK_PAGES)
    fallback_reason = None
    if len(selected) < fallback_target:
        fallback_reason = "no_candidate_page_match" if not selected else "insufficient_candidate_page_context"
        representative: list[tuple[int, int]] = []
        for doc_index in range(len(prompt_docs)):
            doc_keys = page_keys_by_doc.get(doc_index, [])
            if doc_keys:
                representative.append(doc_keys[0])
        if all_keys:
            representative.extend([all_keys[len(all_keys) // 2], all_keys[-1]])
        representative.extend(all_keys)
        for key in representative:
            if len(selected) >= fallback_target:
                break
            add_key(key, "bounded_fallback")

    return {
        "selected": selected,
        "reasons": reasons,
        "field_page_rankings": field_page_rankings,
        "unresolved_sources": unresolved_sources,
        "source_keys": source_keys,
        "selected_before_fallback": selected_before_fallback,
        "fallback_reason": fallback_reason,
    }
