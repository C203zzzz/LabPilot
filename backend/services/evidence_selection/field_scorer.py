"""Field-based retrieval scoring for evidence selection."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from backend.services.text_utils import search_text
from backend.services.parameter_extraction import CANONICAL_FIELD_NAMES


# Field retrieval constants（与拆分前 deepseek_analysis 的取值逐字一致：
# 分片长度决定一段参数块会不会被切开，取值变小会让长参数块丢失检索命中）
_FIELD_RETRIEVAL_CHUNK_CHARS = 1_600
_FIELD_RETRIEVAL_CHUNK_OVERLAP = 200
_NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?")


# Field retrieval terms mapping（与拆分前 deepseek_analysis 的词表逐字一致：删词等于
# 让对应字段再也检索不到证据，"volume/容积"这类词曾被漏掉）
_FIELD_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "product_identity_and_configuration": (
        "model", "catalog number", "order number", "configuration", "series",
        "型号", "货号", "订货号", "配置", "系列",
    ),
    "core_function_and_applications": (
        "function", "application", "applications", "intended use", "purpose",
        "功能", "应用", "用途", "适用范围",
    ),
    "detection_or_processing_modes": (
        "detection mode", "processing mode", "measurement mode", "method",
        "technology", "principle", "检测模式", "处理模式", "测量模式", "方法",
        "技术原理", "工作原理",
    ),
    "capacity_and_throughput": (
        "throughput", "capacity", "volume", "samples per hour", "tests per hour", "batch",
        "speed", "通量", "处理量", "容量", "容积", "每小时", "批处理", "速度",
    ),
    "sample_and_format_compatibility": (
        "sample type", "specimen", "format", "compatible", "compatibility",
        "sample volume", "样本类型", "样品类型", "兼容", "格式", "样本体积", "进样量",
    ),
    "performance_range": (
        "performance", "measurement range", "dynamic range", "range", "minimum",
        "maximum", "resolution", "性能", "量程", "动态范围", "范围", "最小", "最大", "分辨率",
    ),
    "accuracy_sensitivity_and_repeatability": (
        "accuracy", "sensitivity", "precision", "repeatability", "reproducibility",
        "coefficient of variation", "limit of detection", "lod", "准确度", "灵敏度",
        "精度", "重复性", "重现性", "变异系数", "检出限",
    ),
    "temperature_environment_and_safety": (
        "operating temperature", "temperature", "humidity", "environment", "safety",
        "noise", "温度", "湿度", "环境条件", "安全", "防护", "噪声",
    ),
    "software_data_and_compliance": (
        "software", "data export", "data management", "interface", "connectivity",
        "network", "compliance", "certification", "软件", "数据导出", "数据管理",
        "接口", "联网", "合规", "认证",
    ),
    "accessories_consumables_and_options": (
        "accessories", "consumables", "options", "included", "optional", "reagent",
        "配件", "耗材", "选件", "标配", "选配", "试剂",
    ),
    "physical_dimensions_power_and_installation": (
        "dimensions", "dimension", "weight", "power", "voltage", "installation",
        "footprint", "尺寸", "外形", "重量", "功率", "电源", "电压", "安装", "占地",
    ),
    "price_warranty_delivery_and_service": (
        "price", "quotation", "warranty", "delivery", "lead time", "service",
        "support", "training", "价格", "报价", "保修", "质保", "交付", "货期", "售后",
        "服务", "培训",
    ),
}


def _retrieval_text_chunks(text: Any) -> list[tuple[int, int, str]]:
    """Split text into overlapping chunks for field retrieval."""
    value = str(text or "")
    if not value:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + _FIELD_RETRIEVAL_CHUNK_CHARS)
        chunks.append((start, end, value[start:end]))
        if end == len(value):
            break
        start = max(start + 1, end - _FIELD_RETRIEVAL_CHUNK_OVERLAP)
    return chunks


def _retrieval_term_matches(normalized_text: str, compact_text: str, raw_term: str) -> bool:
    """Check if a retrieval term matches in the normalized text."""
    term = search_text(raw_term)
    if not term:
        return False
    compact = term.replace(" ", "")
    if any("一" <= char <= "鿿" for char in compact):
        return compact in compact_text
    return f" {term} " in f" {normalized_text} "


def _field_scores_for_text(text: Any) -> dict[str, dict[str, Any]]:
    """Calculate field scores for a text chunk."""
    normalized = search_text(text)
    if not normalized:
        return {}
    compact = normalized.replace(" ", "")
    contains_number = bool(_NUMERIC_TOKEN_RE.search(unicodedata.normalize("NFKC", str(text))))
    output: dict[str, dict[str, Any]] = {}
    for field_name in CANONICAL_FIELD_NAMES:
        matched_terms = [
            term for term in _FIELD_RETRIEVAL_TERMS[field_name]
            if _retrieval_term_matches(normalized, compact, term)
        ]
        if not matched_terms:
            continue
        # Longer and multi-word terms are more discriminative. A small numeric
        # bonus favors specification blocks over prose without making numbers a
        # standalone retrieval signal.
        score = sum(
            10 + min(12, len(search_text(term).replace(" ", "")))
            + (8 if " " in search_text(term) else 0)
            for term in matched_terms
        )
        if contains_number:
            score += 6
        output[field_name] = {
            "score": score,
            "matched_terms": sorted(set(matched_terms)),
        }
    return output


def field_retrieval_profile(text: Any) -> dict[str, Any]:
    """Generate field retrieval profile for text with chunk-level details."""
    field_scores: dict[str, int] = {}
    matched_terms: defaultdict[str, set[str]] = defaultdict(set)
    chunks: list[dict[str, Any]] = []
    for chunk_index, (start, end, chunk_text) in enumerate(_retrieval_text_chunks(text)):
        matches = _field_scores_for_text(chunk_text)
        if not matches:
            continue
        chunk_score = 0
        chunk_fields: dict[str, int] = {}
        chunk_terms: dict[str, list[str]] = {}
        for field_name, match in matches.items():
            score = int(match["score"])
            field_scores[field_name] = max(field_scores.get(field_name, 0), score)
            matched_terms[field_name].update(match["matched_terms"])
            chunk_score += score
            chunk_fields[field_name] = score
            chunk_terms[field_name] = list(match["matched_terms"])
        chunks.append({
            "chunk_index": chunk_index,
            "char_start": start,
            "char_end": end,
            "score": chunk_score,
            "field_scores": chunk_fields,
            "matched_terms": chunk_terms,
        })
    return {
        "score": sum(field_scores.values()),
        "field_scores": field_scores,
        "matched_terms": {
            field_name: sorted(terms) for field_name, terms in matched_terms.items()
        },
        "chunks": sorted(chunks, key=lambda chunk: (-chunk["score"], chunk["char_start"])),
    }
