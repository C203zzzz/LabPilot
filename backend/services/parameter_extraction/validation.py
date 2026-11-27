"""Parameter validation and status checking utilities."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping


# Measurement value pattern
_MEASUREMENT_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?\s*"
    r"(?:[±+−-]\s*\d+(?:[.,]\d+)?)?\s*[)）]?\s*"
    r"(?:[A-Za-z/%μµ℃°×*·+-]+(?:\s*[A-Za-z0-9/%μµ℃°×*·+-]+)?)",
    re.IGNORECASE,
)

# Comparable field names (fields used for product comparison)
_COMPARABLE_FIELD_NAMES = frozenset({
    "detection_or_processing_modes", "capacity_and_throughput",
    "sample_and_format_compatibility", "performance_range",
    "accuracy_sensitivity_and_repeatability",
    "temperature_environment_and_safety",
    "physical_dimensions_power_and_installation",
})


_UNKNOWN_VALUES = {"", "unknown", "none", "null", "未知", "未提供", "n/a", "na"}

def has_comparable_parameter(configuration: Any, fields: Any) -> bool:
    """Return whether the result contains a value usable for comparison.

    Identity, compliance, accessory and service prose must not satisfy this
    gate merely because it has sources or a non-empty string.
    """
    if _MEASUREMENT_VALUE_RE.search(str(configuration or "")):
        return True
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, Mapping):
            continue
        value = field.get("value")
        if str(field.get("field_name") or "") not in _COMPARABLE_FIELD_NAMES:
            continue
        if not has_usable_parameter([field]):
            continue
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
        text += " " + str(field.get("unit") or "")
        if _MEASUREMENT_VALUE_RE.search(text):
            return True
    return False


def has_usable_parameter(fields: Any) -> bool:
    """这条候选身上有没有**能用**的参数值（与下游取数认同一件事）。

    与 ``drive.py`` 的 ``candidate_pairs`` 同一个判据：字典形态的字段非空即算有值，
    字符串形态的不是 ``unknown`` 一类占位词才算有值。全 ``unknown`` 的补空字段不算。
    """
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, Mapping):
            continue
        value = field.get("value")
        if isinstance(value, Mapping):
            if value:
                return True
            continue
        if isinstance(value, (list, tuple)):
            if value:
                return True
            continue
        if isinstance(value, str) and value.strip().casefold() not in _UNKNOWN_VALUES:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False
