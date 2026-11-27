"""Candidate ownership, sibling merging, brand fallback, and identity tidying."""
from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from typing import Any, Mapping

from backend.services.analysis_messages import (
    no_owned_evidence_failure,
    no_owned_evidence_gap,
    sibling_merge_failure,
    sibling_merge_gap,
)
from backend.services.analysis_prompts import CANONICAL_FIELD_NAMES
from backend.services.parameter_extraction import (
    PARAMETERS_STATUS_EXTRACTED,
    PARAMETERS_STATUS_INCOMPLETE,
    has_usable_parameter,
)
from backend.services.text_utils import table_rows_text as _table_rows_text

PARAMETERS_STATUS_NOT_LOCATED = "not_located"

_MODEL_TAIL_NUMBER_RE = re.compile(r"\d+$")
_NON_MODEL_PREFIX_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_TRADEMARK_RE = re.compile(r"[™®©]")
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z]{1,10}[-/]?\d{1,4}[A-Za-z]{0,4}")
_SEPARATOR_ONLY_RE = re.compile(r"^[\s/\\、,，;；（）()\[\]【】×xX*·.\-—~至和及与或\d]*$")


def enterprise_brand_from_documents(
    documents: list[Mapping[str, Any]],
) -> tuple[str | None, list[str]]:
    """Maker named in the document body; returns a brand value only when unique."""
    from backend.services.procurement_rules import (
        brand_from_enterprise_name,
        enterprise_names_in_text,
    )
    names: list[str] = []
    for doc in documents:
        texts: list[str] = []
        pages = doc.get("pages")
        if isinstance(pages, list):
            texts.extend(str(page.get("text", "")) for page in pages if isinstance(page, Mapping))
        tables = doc.get("tables")
        if isinstance(tables, list):
            texts.extend(_table_rows_text(table) for table in tables if isinstance(table, Mapping))
        for text in texts:
            for name in enterprise_names_in_text(text):
                if name not in names:
                    names.append(name)
    if len(names) != 1:
        return None, names
    return brand_from_enterprise_name(names[0]) or None, names


def model_number_and_prefix(model: Any) -> tuple[str, str] | None:
    """Split trailing numeric identifier and prefix (NFKC normalized, exact digits)."""
    text = unicodedata.normalize("NFKC", str(model or "")).strip()
    match = _MODEL_TAIL_NUMBER_RE.search(text)
    if not match:
        return None
    return match.group(0), text[:match.start()]


def _model_prefix_key(prefix: str) -> str:
    return _NON_MODEL_PREFIX_RE.sub("", str(prefix or "").casefold()).strip()


def model_number_agrees(left: Any, right: Any) -> bool:
    """Whether two models share the exact trailing number with compatible prefixes."""
    left_identity = model_number_and_prefix(left)
    right_identity = model_number_and_prefix(right)
    if left_identity is None or right_identity is None:
        return False
    if left_identity[0] != right_identity[0]:
        return False
    left_key = _model_prefix_key(left_identity[1])
    right_key = _model_prefix_key(right_identity[1])
    return left_key == right_key or not left_key or not right_key


def brands_conflict(left: Any, right: Any) -> bool:
    left_text = str(left or "").strip().casefold()
    right_text = str(right or "").strip().casefold()
    if not left_text or not right_text:
        return False
    return left_text != right_text


def candidate_document_ids(candidate: Mapping[str, Any]) -> set[str]:
    return {
        str(source.get("document_id") or "")
        for source in candidate.get("sources") or []
        if isinstance(source, Mapping) and str(source.get("document_id") or "")
    }


def sibling_merge_source(
    index: int,
    candidates: list[Mapping[str, Any]],
    ownership: list[list[Mapping[str, Any]]],
    output: list[Any] | None = None,
) -> int | None:
    """Find an owned sibling candidate with exact matching model number to copy parameters from."""
    candidate = candidates[index]
    document_ids = candidate_document_ids(candidate)
    for other_index, other in enumerate(candidates):
        if other_index == index or not ownership[other_index]:
            continue
        if output and other_index < len(output):
            other_result = output[other_index]
            if isinstance(other_result, dict):
                origin = other_result.get("parameters_origin", {})
                if isinstance(origin, dict) and origin.get("kind") == "sibling_candidate":
                    continue
        if not model_number_agrees(candidate.get("model"), other.get("model")):
            continue
        if brands_conflict(candidate.get("brand"), other.get("brand")):
            continue
        if document_ids:
            owned_ids = {str(doc.get("document_id") or "") for doc in ownership[other_index]}
            if not (document_ids & owned_ids):
                continue
        return other_index
    return None


def merge_unowned_candidates(
    output: list[Any],
    candidates: list[Mapping[str, Any]],
    ownership: list[list[Mapping[str, Any]]],
    gaps: list[str],
    failures: list[dict[str, Any]],
) -> list[Any]:
    """Copy parameters from a same-number owned sibling for unowned candidates and leave traces."""
    for index, candidate in enumerate(candidates):
        if index >= len(output) or ownership[index]:
            continue
        current = output[index]
        if not isinstance(current, dict):
            continue
        if str(current.get("parameters_status") or "") == PARAMETERS_STATUS_INCOMPLETE:
            continue
        if has_usable_parameter(current.get("fields")):
            continue
        source_index = sibling_merge_source(index, candidates, ownership, output)
        if source_index is None or source_index >= len(output):
            gaps.append(no_owned_evidence_gap(candidate.get("model")))
            failures.append(no_owned_evidence_failure(candidate))
            continue
        source = output[source_index]
        if not isinstance(source, dict) or not has_usable_parameter(source.get("fields")):
            gaps.append(no_owned_evidence_gap(candidate.get("model")))
            failures.append(no_owned_evidence_failure(candidate))
            continue
        sibling = candidates[source_index]
        identity = model_number_and_prefix(candidate.get("model"))
        merged = dict(current)
        merged["fields"] = copy.deepcopy(source.get("fields") or [])
        merged["parameters_status"] = PARAMETERS_STATUS_EXTRACTED
        merged["parameters_origin"] = {
            "kind": "sibling_candidate",
            "model": str(sibling.get("model") or ""),
            "number": identity[0] if identity else "",
        }
        output[index] = merged
        gaps.append(sibling_merge_gap(candidate.get("model"), sibling.get("model"), identity[0] if identity else ""))
        failures.append(
            sibling_merge_failure(
                candidate,
                sibling,
                identity[0] if identity else "",
                ownership[source_index],
            )
        )
    return output


def identity_key(value: Any) -> str:
    text = _TRADEMARK_RE.sub("", str(value or ""))
    return re.sub(r"[\s\-_/\\]", "", text).upper()


def family_key(value: Any) -> str:
    return re.sub(r"\d+", "", identity_key(value))


def is_series_named(candidate: Mapping[str, Any]) -> bool:
    model = str(candidate.get("model") or "").strip()
    key = identity_key(model)
    if not key:
        return False
    for other in (candidate.get("series"), candidate.get("brand")):
        other_key = identity_key(other)
        if other_key and (key == other_key or (len(key) >= 4 and key in other_key)):
            return True
    return bool(_TRADEMARK_RE.search(model)) and not re.search(r"\d", model)


def merge_configuration(base: Any, extra: Any) -> str:
    parts, seen = [], set()
    for text in (base, extra):
        for part in re.split(r"[；;]", str(text or "")):
            part = part.strip()
            key = re.sub(r"[\s,，。]", "", part)
            if not part or key in seen:
                continue
            seen.add(key)
            parts.append(part)
    return "；".join(parts)


def evidence_excerpts(candidate: Mapping[str, Any]) -> list[str]:
    texts = []
    for source in candidate.get("sources") or []:
        if isinstance(source, Mapping) and source.get("excerpt"):
            texts.append(str(source["excerpt"]))
    for field in candidate.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        for source in field.get("sources") or []:
            if isinstance(source, Mapping) and source.get("excerpt"):
                texts.append(str(source["excerpt"]))
        for raw in field.get("raw_evidence") or []:
            if isinstance(raw, Mapping) and raw.get("excerpt"):
                texts.append(str(raw["excerpt"]))
    return texts


def family_models_in_excerpt(text: str, known: list) -> list:
    known_keys = [key for key in (identity_key(model) for model in known) if key]
    positions = []
    for model in known:
        for match in re.finditer(re.escape(str(model)), text, re.IGNORECASE):
            positions.append((match.start(), match.end()))
    if not positions:
        return []
    found = []
    for token_match in _MODEL_TOKEN_RE.finditer(text):
        token = token_match.group(0)
        token_key = identity_key(token)
        if not token_key or token_key in known_keys:
            continue
        family = family_key(token)
        if len(family) < 3:
            continue
        adjacent = False
        for start, end in positions:
            if end <= token_match.start():
                between = text[end:token_match.start()]
            elif token_match.end() <= start:
                between = text[token_match.end():start]
            else:
                continue
            same_family = any(family_key(model) == family for model in known if len(family_key(model)) >= 3)
            if same_family and len(between) <= 60 and _SEPARATOR_ONLY_RE.match(between):
                adjacent = True
                break
        if adjacent and token_key not in {identity_key(item) for item in found}:
            found.append(token)
    return found


def tidy_candidate_identities(result: dict) -> None:
    """Fold series-named pseudo-candidates and surface adjacent family models from excerpts."""
    candidates = [c for c in (result.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return
    gaps = result.setdefault("gaps", [])
    kept, series_named = [], []
    for candidate in candidates:
        (series_named if is_series_named(candidate) else kept).append(candidate)
    merged_notes = []
    for lost in series_named:
        owner = None
        for candidate in kept:
            for field in ("series", "brand"):
                lost_key = identity_key(lost.get(field))
                if lost_key and identity_key(candidate.get(field)) == lost_key:
                    owner = candidate
                    break
            if owner is not None:
                break
        if owner is None:
            kept.append(lost)
            continue
        owner["configuration"] = merge_configuration(owner.get("configuration"), lost.get("configuration"))
        merged_notes.append((str(lost.get("model")), str(owner.get("model"))))
    if merged_notes:
        gaps.append(
            "识别到的%s是系列/商标名、不是型号，已并入同一系列的%s。"
            % (
                "、".join("「%s」" % item[0] for item in merged_notes),
                "、".join(item[1] for item in merged_notes),
            )
        )

    known = [str(candidate.get("model") or "") for candidate in kept]
    completed = []
    for owner in list(kept):
        for excerpt in evidence_excerpts(owner):
            for model in family_models_in_excerpt(excerpt, known):
                if identity_key(model) in {identity_key(item) for item in known}:
                    continue
                kept.append({
                    "candidate_id": "analysis-" + uuid.uuid4().hex[:12],
                    "brand": owner.get("brand"),
                    "series": owner.get("series"),
                    "model": model,
                    "configuration": owner.get("configuration"),
                    "status": "pending_review",
                    "sources": [dict(source) for source in (owner.get("sources") or []) if isinstance(source, Mapping)],
                    "fields": [
                        {"field_name": name, "value": "unknown", "unit": None, "status": "unknown", "sources": []}
                        for name in CANONICAL_FIELD_NAMES
                    ],
                    "parameters_status": PARAMETERS_STATUS_NOT_LOCATED,
                    "parameters_origin": None,
                    "completed_from_family": True,
                })
                known.append(model)
                completed.append((model, str(owner.get("model"))))
    if completed:
        gaps.append(
            "资料里的型号串还包含%s（与%s同系列），已一并列为候选；"
            "它们的参数还没抽取，需要人工核对。"
            % (
                "、".join("「%s」" % item[0] for item in completed),
                "、".join(dict.fromkeys(item[1] for item in completed)),
            )
        )
    result["candidates"] = kept
