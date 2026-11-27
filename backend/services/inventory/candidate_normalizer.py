"""Candidate identity and normalization utilities."""
from __future__ import annotations

import re
import unicodedata
from typing import Any
from collections.abc import Mapping

from backend.services.text_utils import has_concrete_model, search_text  # noqa: F401  (has_concrete_model 转出)


# Candidate identity keys in order of specificity
_CANDIDATE_KEYS = ("brand", "series", "model", "configuration")

# Catalog number detection pattern
_CATALOG_HEADER_RE = re.compile(
    r"(?:catalog(?:ue)?\s*(?:number|no\.?|#)|order\s*(?:number|no\.?|#)|"
    r"part\s*(?:number|no\.?|#)|sku|货号|订货号|物料号|订购编号|产品编号)",
    re.IGNORECASE,
)


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

def candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Return normalized identity tuple for a candidate."""
    return tuple(search_text(candidate.get(key)).replace(" ", "") for key in _CANDIDATE_KEYS)


def normalized_model(value: Any) -> str:
    """Return normalized model string for comparison."""
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def compound_model_variants(model: str) -> list[str]:
    """把"两种规格写成一个型号"的写法展开成它覆盖的型号。

    ``SSW-420/600-2S`` -> ``['SSW-420-2S', 'SSW-600-2S']``：按 ``-`` 分段后只展开
    含 ``/`` 的那一段，其余段原样拼回。
    """
    segments = str(model or "").split("-")
    for index, segment in enumerate(segments):
        parts = [part for part in segment.split("/") if part]
        if len(parts) < 2:
            continue
        variants = []
        for part in parts:
            replaced = list(segments)
            replaced[index] = part
            variants.append("-".join(replaced))
        return variants
    return []


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


def configuration_with_catalog_number(configuration: Any, catalog_number: Any) -> str | None:
    configuration_text = str(configuration or "").strip()
    catalog_text = str(catalog_number or "").strip()
    if not catalog_text:
        return configuration_text or None
    if normalized_model(catalog_text) in normalized_model(configuration_text):
        return configuration_text
    catalog_fragment = f"订货号 {catalog_text}"
    return f"{configuration_text}；{catalog_fragment}" if configuration_text else catalog_fragment


def probable_catalog_number(candidate: Mapping[str, Any],
                             evidence_by_location: Mapping[tuple[str, str], str]) -> str | None:
    """Return a model value only when evidence proves it is an ordering code.

    The fallback is deliberately narrow: an explicit catalog_number from the
    model is accepted, otherwise the page needs an ordering-number header and
    the alleged model must be a longer, code-like extension of a concrete
    series value. This avoids category- or manufacturer-specific blacklists.
    """
    explicit = str(candidate.get("catalog_number") or "").strip()
    if explicit and has_concrete_model(explicit):
        return explicit
    model = str(candidate.get("model") or "").strip()
    series = str(candidate.get("series") or "").strip()
    model_key = normalized_model(model)
    series_key = normalized_model(series)
    if not model_key or not series_key or model_key == series_key or not model_key.startswith(series_key):
        return None
    digit_count = sum(character.isdigit() for character in model)
    separated_numeric_groups = len(re.findall(r"\d+", model)) >= 3
    code_like = separated_numeric_groups or digit_count >= 7
    if not code_like:
        return None
    for source in candidate.get("sources", []) if isinstance(candidate.get("sources"), list) else []:
        if not isinstance(source, Mapping):
            continue
        key = (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        evidence = evidence_by_location.get(key, "")
        if model_key in normalized_model(evidence) and _CATALOG_HEADER_RE.search(evidence):
            return model
    return None


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
