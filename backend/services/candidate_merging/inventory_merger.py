"""Main candidate inventory merger with filtering and deduplication."""
from __future__ import annotations

import re
from typing import Any, Mapping

from backend.infrastructure.deepseek import DeepSeekError
from backend.services.candidate_service import measurement_value_not_model
from backend.services.evidence_validation import validated_sources, search_text, normalized_model
from backend.services.inventory import (
    compound_model_variants,
    configuration_with_catalog_number,
    probable_catalog_number,
)
from backend.services.text_utils import has_concrete_model
from backend.services.candidate_merging.configuration_merge import (
    merge_configuration as _merge_configuration,
)
from backend.services.candidate_merging.identity_matcher import (
    candidate_identity,
    compatible_candidate_identity,
)
from backend.services.candidate_merging.series_alias_resolver import merge_short_series_aliases


#: 每条候选最多保留几条来源（与拆分前 deepseek_analysis 的 _INVENTORY_SOURCE_LIMIT 一致）。
_INVENTORY_SOURCE_LIMIT = 8


_ACCESSORY_CONTEXT_RE = re.compile(
    r"(?:accessor(?:y|ies)|adapter|rotor|rack|stand|holder|module|plate|"
    r"配件|附件|适配器|转子|支架|模块|微量板)",
    re.IGNORECASE,
)
_COMPATIBILITY_CONTEXT_RE = re.compile(
    r"(?:compatible\s+with|suitable\s+for|for\s+(?:the\s+)?(?:following|all)|"
    r"适用于|兼容|配套)",
    re.IGNORECASE,
)
_CROSS_PROMOTION_CONTEXT_RE = re.compile(
    r"(?:(?:besides|in addition to|除了).{0,180}(?:also|another|还有|另有).{0,180}"
    r"(?:more\s+information|learn\s+more|更多信息|详情)|"
    r"(?:more\s+information|learn\s+more|更多信息|详情).{0,180}"
    r"(?:besides|in addition to|除了).{0,180}(?:also|another|还有|另有))",
    re.IGNORECASE | re.DOTALL,
)
_NON_INSTRUMENT_ROLE_RE = re.compile(
    r"(?:software|软件|consumable|耗材|reagent|试剂|accessor(?:y|ies)|配件|附件|"
    r"adapter|适配器|rotor|转子|rack|stand|holder|支架|module|模块|"
    r"assay(?:\s+or\s+setting)?|setting|preset|检测方法|检测设置|界面预设|"
    r"cross[ _-]?promotion|交叉推广)",
    re.IGNORECASE,
)
_PACKAGING_CONTEXT_RE = re.compile(
    r"(?:individually\s+packaged|disposable|pack(?:ed|aging)?|box\b|"
    r"单独包装|一次性|包装|\d+\s*(?:片|个|支|盒|包|枚)\s*/\s*(?:箱|盒|包))",
    re.IGNORECASE,
)
_SETTING_UI_CONTEXT_RE = re.compile(
    r"(?:measurement\s+wavelength|reference\s+well|bandwidth|input\s+data|"
    r"测量波长|参比孔|带宽|输入数据)",
    re.IGNORECASE,
)

def merge_inventory_candidates(results: list[Mapping[str, Any]],
                                 evidence_by_location: Mapping[tuple[str, str], str]) -> tuple[list[dict[str, Any]], str | None]:
    merged: list[dict[str, Any]] = []
    identities: list[tuple[str, ...]] = []
    source_keys: list[set[tuple[str, str, str]]] = []
    catalog_derived: list[bool] = []
    #: 这一批报上来的全部型号（含还没合并的）。用来认出"两种规格写成一个型号"的写法。
    known_models = [str(item.get("model") or "") for result in results
                    for item in (result.get("candidates") or []) if isinstance(item, Mapping)]
    for result in results:
        raw_candidates = result.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise DeepSeekError("invalid_candidate_inventory", "候选清单格式无效")
        for raw_item in raw_candidates:
            raw = normalise_inventory_candidate(raw_item, evidence_by_location) if isinstance(raw_item, Mapping) else raw_item
            if not isinstance(raw, Mapping) or not has_concrete_model(raw.get("model")):
                continue
            # 合体型号不是独立产品：它展开出来的每个型号都已经是别的候选时，再收一条
            # 就是凭空多一个不存在的产品（水槽的 ``SSW-420/600-2S``）。
            if is_compound_of_known_models(str(raw.get("model") or ""), known_models):
                continue
            product_role = search_text(raw.get("product_role")).replace(" ", "_")
            if product_role in {
                "accessory", "consumable", "reagent", "software", "service",
                "module", "adapter", "rotor", "plate",
            }:
                continue
            validated_raw_sources = validated_sources(raw.get("sources", []), evidence_by_location)
            if _accessory_compatibility_only_sources(validated_raw_sources):
                continue
            if _candidate_has_only_non_instrument_evidence(
                raw, validated_raw_sources, evidence_by_location,
            ):
                continue
            # A specification row ("| 电源 | AC 220±22V 50/60Hz |") is a value,
            # not a product. The model may still quote one as a model, so the
            # same rule the local extractor uses is applied to its evidence.
            if measurement_value_not_model(
                str(raw.get("model") or ""),
                " ".join(str(source.get("excerpt") or "") for source in validated_raw_sources),
            ):
                continue
            identity = candidate_identity(raw)
            exact_matches = [index for index, existing in enumerate(identities) if existing == identity]
            same_model_matches = [
                index for index, existing in enumerate(identities)
                if normalized_model(existing[2]) == normalized_model(identity[2])
                and (not existing[0] or not identity[0] or existing[0] == identity[0])
            ]
            compatible_matches = [
                index for index, existing in enumerate(identities)
                if compatible_candidate_identity(existing, identity)
            ]
            same_instrument_matches = [
                index for index, existing in enumerate(identities)
                if (raw.get("_catalog_derived") or catalog_derived[index])
                and normalized_model(existing[2]) == normalized_model(identity[2])
                and (not existing[0] or not identity[0] or existing[0] == identity[0])
                and (not existing[1] or not identity[1] or existing[1] == identity[1])
            ]
            if exact_matches:
                candidate_index = exact_matches[0]
            elif len(same_model_matches) == 1:
                candidate_index = same_model_matches[0]
            elif len(compatible_matches) == 1:
                candidate_index = compatible_matches[0]
            elif len(same_instrument_matches) == 1:
                candidate_index = same_instrument_matches[0]
            else:
                candidate_index = len(merged)
                merged.append({
                    "brand": raw.get("brand"),
                    "series": raw.get("series"),
                    "model": raw.get("model"),
                    "configuration": raw.get("configuration"),
                    "sources": [],
                })
                identities.append(identity)
                source_keys.append(set())
                catalog_derived.append(bool(raw.get("_catalog_derived")))
            candidate = merged[candidate_index]
            for key in ("brand", "series", "model"):
                if candidate.get(key) in (None, "") and raw.get(key) not in (None, ""):
                    candidate[key] = raw.get(key)
            candidate["configuration"] = _merge_configuration(
                candidate.get("configuration"), raw.get("configuration"),
            )
            catalog_derived[candidate_index] = catalog_derived[candidate_index] or bool(raw.get("_catalog_derived"))
            identities[candidate_index] = candidate_identity(candidate)
            for normalized in validated_raw_sources:
                source_key = (
                    str(normalized.get("document_id") or ""),
                    str(normalized.get("page_or_location") or ""),
                    search_text(normalized.get("excerpt")),
                )
                if source_key in source_keys[candidate_index]:
                    continue
                source_keys[candidate_index].add(source_key)
                if len(candidate["sources"]) < _INVENTORY_SOURCE_LIMIT:
                    candidate["sources"].append(normalized)
    merged = merge_short_series_aliases(merged)
    truncation_gap = None
    if len(merged) > 50:
        original_count = len(merged)

        def identity_score(candidate: dict[str, Any]) -> int:
            """Score candidate by identity completeness: brand+1, series+1, model+2."""
            score = 0
            if candidate.get("brand"):
                score += 1
            if candidate.get("series"):
                score += 1
            if candidate.get("model"):
                score += 2
            return score

        merged = sorted(merged, key=identity_score, reverse=True)[:50]
        truncation_gap = f"候选数 {original_count} 超过上限，已保留评分前 50 个"
    return merged, truncation_gap


def is_compound_of_known_models(model: str, known_models) -> bool:
    """这个型号是不是"已知型号的组合"——是的话它不该占一个候选位。

    资料封面常把两种规格并排写成一个型号（``SSW-420/600-2S``），模型据此又报一条
    候选，于是清单里多出一个并不存在的产品（2026-10-03 批2 的水槽：3 条候选里 1 条
    就是它，参数抽取还为它白跑了一轮）。

    只在**展开出来的型号都已经是别的候选**时才合并：``AX224/E`` 是 OHAUS 的真实型号
    写法，展开出的 ``E`` 不在清单里，照旧当独立候选。
    """
    variants = compound_model_variants(model)
    if len(variants) < 2:
        return False
    own = normalized_model(model)
    others = {normalized_model(value) for value in known_models
              if normalized_model(value) and normalized_model(value) != own}
    return all(normalized_model(variant) in others for variant in variants)


def normalise_inventory_candidate(candidate: Mapping[str, Any],
                                   evidence_by_location: Mapping[tuple[str, str], str]) -> dict[str, Any]:
    normalized = dict(candidate)
    catalog_number = probable_catalog_number(normalized, evidence_by_location)
    if not catalog_number:
        normalized["_catalog_derived"] = False
        return normalized
    if normalized_model(normalized.get("model")) == normalized_model(catalog_number):
        series = normalized.get("series")
        if not has_concrete_model(series):
            normalized["_catalog_derived"] = False
            return normalized
        normalized["model"] = series
    normalized["configuration"] = configuration_with_catalog_number(
        normalized.get("configuration"), catalog_number,
    )
    normalized["_catalog_derived"] = True
    return normalized

def _accessory_compatibility_only_sources(sources: list[Mapping[str, Any]]) -> bool:
    if not sources:
        return False
    return all(
        _ACCESSORY_CONTEXT_RE.search(str(source.get("excerpt") or ""))
        and _COMPATIBILITY_CONTEXT_RE.search(str(source.get("excerpt") or ""))
        for source in sources
    )
def _candidate_has_only_non_instrument_evidence(
    candidate: Mapping[str, Any],
    sources: list[Mapping[str, Any]],
    evidence_by_location: Mapping[tuple[str, str], str],
) -> bool:
    """Reject role-confused seeds using their own evidence, not product names."""
    role = search_text(candidate.get("product_role")).replace(" ", "_")
    if role and role not in {"instrument", "instrument_configuration"}:
        return bool(_NON_INSTRUMENT_ROLE_RE.search(role))
    if not sources:
        return False
    identifier = str(candidate.get("model") or candidate.get("series") or "").strip()
    identifier_key = normalized_model(identifier)

    def source_is_non_instrument(source: Mapping[str, Any]) -> bool:
        excerpt = str(source.get("excerpt") or "")
        evidence = evidence_by_location.get((
            str(source.get("document_id") or ""),
            str(source.get("page_or_location") or ""),
        ), "")
        predecessor = re.escape(identifier) + r'\s*(?:的)?(?:升级款|升级版|后继型号|后继产品|换代产品)'
        if identifier and re.search(predecessor, excerpt, re.I):
            return True
        if re.search(r"(?:software|软件)", excerpt, re.IGNORECASE):
            return True
        if _PACKAGING_CONTEXT_RE.search(excerpt) and re.search(
            r"(?:chip|disc|patch|plate|cartridge|tip|芯片|圆盘|贴片|板|卡匣|吸头)",
            excerpt, re.IGNORECASE,
        ):
            return True
        if re.search(r"\bname\s+" + re.escape(identifier) + r"\b", excerpt, re.IGNORECASE):
            if _SETTING_UI_CONTEXT_RE.search(evidence):
                return True
        if len(sources) == 1 and identifier_key and identifier_key in normalized_model(evidence):
            if _CROSS_PROMOTION_CONTEXT_RE.search(evidence):
                return True
        return False

    return all(source_is_non_instrument(source) for source in sources)
