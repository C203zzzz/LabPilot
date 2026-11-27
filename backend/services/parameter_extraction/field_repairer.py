"""Field repair and gap detection utilities."""
from __future__ import annotations

import unicodedata
from collections import defaultdict
from typing import Any, Mapping

from backend.services.evidence_validation import (
    temperature_metric,
    validated_field,
    value_identity,
)


# Canonical field names expected in all candidates（与拆分前 deepseek_analysis 的
# 12 个字段、同顺序一致。曾漏掉 core_function_and_applications /
# detection_or_processing_modes / accessories_consumables_and_options 三个字段，
# 而 evidence_selection 又用这份清单遍历字段评分——那三个字段就永远检索不到证据。）
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


def repairable_field_names(raw_fields: Any, evidence_payload: Mapping[str, Any]) -> list[str]:
    if not isinstance(raw_fields, list):
        return []
    fields_by_name = {
        str(field.get("field_name")): field
        for field in raw_fields if isinstance(field, Mapping)
    }
    if not set(CANONICAL_FIELD_NAMES).issubset(fields_by_name):
        return []
    supported_field_count = sum(
        isinstance(field.get("sources"), list) and bool(field.get("sources"))
        for field in fields_by_name.values()
    )
    if supported_field_count == 0:
        return []
    selection = evidence_payload.get("evidence_selection", {})
    coverage = selection.get("field_coverage", {}) if isinstance(selection, Mapping) else {}
    repairable = []
    for field_name in CANONICAL_FIELD_NAMES:
        field = fields_by_name[field_name]
        field_coverage = coverage.get(field_name, {}) if isinstance(coverage, Mapping) else {}
        if not isinstance(field_coverage, Mapping) or not field_coverage.get("covered"):
            continue
        value = str(field.get("value", "")).strip().casefold()
        has_sources = isinstance(field.get("sources"), list) and bool(field.get("sources"))
        if field.get("_unsupported_parts") or not has_sources or value in {"", "unknown", "none", "null", "未知", "未提供"}:
            repairable.append(field_name)
    return repairable


def merge_repaired_fields(raw_fields: list[Any], repaired_fields: Any,
                           requested_fields: list[str],
                           evidence_by_location: Mapping[tuple[str, str], str]) -> list[Any]:
    replacements: dict[str, dict[str, Any]] = {}
    allowed = set(requested_fields)
    for raw in repaired_fields if isinstance(repaired_fields, list) else []:
        if not isinstance(raw, Mapping):
            continue
        field_name = str(raw.get("field_name", "")).strip()
        if field_name not in allowed:
            continue
        normalized = validated_field(raw, evidence_by_location)
        if normalized["sources"]:
            normalized["status"] = "pending_review"
            replacements[field_name] = normalized
    if not replacements:
        return raw_fields
    merged = []
    for field in raw_fields:
        field_name = str(field.get("field_name", "")) if isinstance(field, Mapping) else ""
        replacement = replacements.get(field_name)
        if replacement and isinstance(field.get("value"), Mapping) and isinstance(replacement.get("value"), Mapping):
            # A repair cannot silently discard independently supported earlier values.
            combined = {**field["value"], **replacement["value"]}
            combined_sources = []
            for source in [*field.get("sources", []), *replacement.get("sources", [])]:
                if source not in combined_sources:
                    combined_sources.append(source)
            replacement = validated_field({**replacement, "value": combined, "sources": combined_sources}, evidence_by_location)
            unresolved_values = {**field.get("_unsupported_values", {}),
                                 **replacements[field_name].get("_unsupported_values", {}),
                                 **replacement.get("_unsupported_values", {})}
            retained_values = replacement["value"] if isinstance(replacement["value"], Mapping) else {}
            # Keep signs, decimals and comparison symbols: -5 and +5 are not
            # the same repaired value, even when the labels have changed.
            def exact_value_key(value):
                return ''.join(unicodedata.normalize('NFKC', value_identity(value)).casefold().split())
            resolved = {name for name, value in unresolved_values.items()
                        if any(exact_value_key(value) == exact_value_key(v)
                               and (not temperature_metric(name) or temperature_metric(name) == temperature_metric(k))
                               for k, v in retained_values.items())}
            replacement["_unsupported_parts"] = sorted(set(
                field.get("_unsupported_parts", []) + replacements[field_name].get("_unsupported_parts", [])
                + replacement.get("_unsupported_parts", [])
            ) - set(retained_values) - resolved)
            replacement["_unsupported_values"] = {name: value for name, value in unresolved_values.items()
                                                  if name in replacement["_unsupported_parts"]}
        merged.append(replacement or field)
    return merged
