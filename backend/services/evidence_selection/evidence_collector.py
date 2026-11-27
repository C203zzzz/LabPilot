"""Evidence collection and indexing for candidate analysis."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Mapping

from backend.services.text_utils import (
    scalar_texts as _scalar_texts,
)

# Import from evidence_selection to avoid circular imports
from backend.services.evidence_selection.field_scorer import (
    field_retrieval_profile as _field_retrieval_profile,
)
from backend.services.evidence_selection.candidate_scorer import (
    matched_candidate_terms as _matched_candidate_terms,
)
from backend.services.text_utils import table_rows_text as _table_rows_text


# 本地定义_page_number以避免循环导入
_PAGE_LOCATION_RE = re.compile(r"(?:^|\b)page:(\d+)(?:\b|/)", re.IGNORECASE)


def _page_number(location: Any) -> int | None:
    """Extract page number from various location formats."""
    if isinstance(location, int):
        return location if location > 0 else None
    text = str(location or "").strip()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    match = _PAGE_LOCATION_RE.search(text)
    return int(match.group(1)) if match else None


def collect_evidence_units(
    prompt_docs: list[dict[str, Any]],
    candidate_terms: list[tuple[str, str, str, int]],
) -> dict[str, Any]:
    """
    收集所有页面和表格的证据单元，计算检索profile和评分。

    参数:
        prompt_docs: 提示文档列表
        candidate_terms: 候选品术语列表

    返回:
        包含以下键的字典:
        - all_keys: 所有页面key列表
        - page_by_key: 页面内容映射
        - page_keys_by_doc: 每个文档的页面key列表
        - page_lookup: 按(doc_index, page_number)查找页面key
        - reasons: 页面选择理由集合
        - scores: 候选品匹配分数
        - page_retrieval_profiles: 页面检索profile
        - table_entries: 表格条目列表
    """
    all_keys: list[tuple[int, int]] = []
    page_by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    page_keys_by_doc: dict[int, list[tuple[int, int]]] = {}
    page_lookup: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    reasons: dict[tuple[int, int], set[str]] = defaultdict(set)
    scores: dict[tuple[int, int], int] = defaultdict(int)
    page_retrieval_profiles: dict[tuple[int, int], dict[str, Any]] = {}
    table_entries: list[dict[str, Any]] = []

    for doc_index, doc in enumerate(prompt_docs):
        doc_keys: list[tuple[int, int]] = []
        tables_by_page: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
        raw_tables = doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []

        # 收集表格条目
        for table_position, table in enumerate(raw_tables):
            if not isinstance(table, Mapping):
                continue
            table_page = _page_number(table.get("source_location"))
            table_text = _table_rows_text(table)
            entry = {
                "key": (doc_index, table_position),
                "doc_index": doc_index,
                "table_position": table_position,
                "table": table,
                "page": table_page,
                "location": table.get("source_location") or f"table:{table.get('index', 'unknown')}",
                "profile": _field_retrieval_profile(table_text),
                "candidate_score": sum(score for _, score in _matched_candidate_terms(table_text, candidate_terms)),
            }
            table_entries.append(entry)
            if table_page is not None:
                tables_by_page[table_page].append(entry)

        # 收集页面并计算检索profile
        for page_index, page in enumerate(doc.get("pages", [])):
            if not isinstance(page, Mapping):
                continue
            key = (doc_index, page_index)
            all_keys.append(key)
            doc_keys.append(key)
            page_by_key[key] = page
            number = int(page.get("page") or page_index + 1)
            page_lookup[(doc_index, number)].append(key)

            # 构建可搜索文本
            searchable = str(page.get("text", ""))
            retrieval_text = searchable
            if page.get("visual_fields"):
                searchable += "\n" + json.dumps(page.get("visual_fields"), ensure_ascii=False, default=str)
                retrieval_text += "\n" + "\n".join(_scalar_texts(page.get("visual_fields")))

            # 计算页面profile
            page_profile = _field_retrieval_profile(retrieval_text)
            combined_field_scores = dict(page_profile["field_scores"])
            combined_matched_terms: defaultdict[str, set[str]] = defaultdict(set)
            for field_name, matched in page_profile["matched_terms"].items():
                combined_matched_terms[field_name].update(matched)

            # 合并表格的字段分数
            for table_entry in tables_by_page.get(number, []):
                table_profile = table_entry["profile"]
                searchable += "\n" + _table_rows_text(table_entry["table"])
                for field_name, score in table_profile["field_scores"].items():
                    combined_field_scores[field_name] = max(
                        combined_field_scores.get(field_name, 0), int(score),
                    )
                for field_name, matched in table_profile["matched_terms"].items():
                    combined_matched_terms[field_name].update(matched)

            # 构建最终profile
            page_retrieval_profiles[key] = {
                **page_profile,
                "page_score": page_profile["score"],
                "page_field_scores": page_profile["field_scores"],
                "page_matched_terms": page_profile["matched_terms"],
                "score": sum(combined_field_scores.values()),
                "field_scores": combined_field_scores,
                "matched_terms": {
                    field_name: sorted(matched)
                    for field_name, matched in combined_matched_terms.items()
                },
            }

            # 记录字段匹配理由
            for field_name in combined_field_scores:
                reasons[key].add(f"canonical_field_match:{field_name}")

            # 计算候选品匹配分数
            for field, score in _matched_candidate_terms(searchable, candidate_terms):
                reasons[key].add(f"candidate_{field}_match")
                scores[key] += score

        page_keys_by_doc[doc_index] = doc_keys

    return {
        "all_keys": all_keys,
        "page_by_key": page_by_key,
        "page_keys_by_doc": page_keys_by_doc,
        "page_lookup": page_lookup,
        "reasons": reasons,
        "scores": scores,
        "page_retrieval_profiles": page_retrieval_profiles,
        "table_entries": table_entries,
    }
