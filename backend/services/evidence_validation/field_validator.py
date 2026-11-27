"""Field validation with source verification and unit checking."""
from __future__ import annotations

from typing import Any, Mapping

from backend.services.evidence_validation.excerpt_validator import (
    validated_sources,
    excerpt_matches_evidence,
    _source,
)
from backend.services.evidence_validation.numeric_matching import numeric_tokens
from backend.services.evidence_validation.text_normalization import scalar_texts
from backend.services.evidence_validation.quantity_unit_checker import missing_quantity_unit
from backend.services.evidence_validation.domain_validators import metric_excerpt_supports


_FIELD_SOURCE_LIMIT = 24


def validated_field(
    raw: Mapping[str, Any],
    evidence_by_location: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    """Retain supported independent parameters; never split values from conditions.

    A mapping entry is atomic, including any nested value/unit/condition object.
    Unsupported string fragments cannot safely be separated and remain unknown.
    The source and numeric checks are identical to the whole-field guard.
    """
    field = dict(raw)

    # Keep a bounded, location-checked copy of the model's citations even when
    # strict value validation rejects the structured value. The initial
    # delivery must remain inspectable: an unparsed value is not a confirmed
    # fact, but it is also not equivalent to having no source at all.
    raw_evidence = []
    for raw_source in raw.get("sources", []) if isinstance(raw.get("sources", []), list) else []:
        source = _source(raw_source)
        key = (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        evidence = evidence_by_location.get(key)
        if evidence is None:
            continue
        source["excerpt"] = str(evidence)[:2000]
        source["evidence_status"] = "unparsed"
        raw_evidence.append(source)
        if len(raw_evidence) >= _FIELD_SOURCE_LIMIT:
            break
    if raw_evidence:
        field["_raw_evidence"] = raw_evidence

    sources = validated_sources(raw.get("sources", []), evidence_by_location)[:_FIELD_SOURCE_LIMIT]
    value = raw.get("value", "unknown")
    supported = validated_sources(sources, evidence_by_location, required_value=value)
    missing = []

    if not supported:
        if isinstance(value, Mapping) and sources:
            kept = {}
            for name, part in value.items():
                # Without numbers, an unrelated string must not hitchhike on a
                # sibling's numeric citation when salvaging a rejected group.
                directly_cited = bool(numeric_tokens(part)) or all(
                    any(excerpt_matches_evidence(text, source["excerpt"]) for source in sources)
                    for text in scalar_texts(part)
                ) and bool(scalar_texts(part))
                if directly_cited and validated_sources(sources, evidence_by_location, required_value=part):
                    kept[name] = part
                else:
                    missing.append(str(name))
            value = kept or "unknown"
            supported = sources if kept else []
        elif str(value).strip().casefold() not in {"", "unknown", "none", "null", "未知", "未提供"}:
            missing = ["整项"]
        if not supported:
            value = "unknown"
            field["unit"] = None

    if isinstance(value, Mapping):
        kept = {}
        for name, part in value.items():
            if metric_excerpt_supports(str(name), part, sources) and not missing_quantity_unit(
                str(name), part, raw
            ):
                kept[name] = part
            else:
                missing.append(str(name))
        value = kept or "unknown"
        if not kept:
            supported = []
            field["unit"] = None

    field.update(value=value, sources=supported)
    field["_unsupported_parts"] = missing
    field["_unsupported_values"] = {
        name: raw.get("value", {}).get(name) if isinstance(raw.get("value"), Mapping) else raw.get("value")
        for name in missing
    }
    if not supported:
        field["status"] = "unknown"

    return field
