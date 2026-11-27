"""Deterministic parameter extraction from explicit parameter lines."""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from backend.services.evidence_validation import (
    validated_field,
    source_evidence_index,
)
from backend.services.parameter_extraction.validation import has_comparable_parameter
from backend.services.procurement_rules import contains_model


logger = logging.getLogger(__name__)


# Regex pattern for deterministic parameter line matching
_DETERMINISTIC_PARAMETER_LINE_RE = re.compile(
    r"(?P<label>环境温度|相对湿度|大气压力|控温范围|温度波动度|上偏差报警|定时范围|"
    r"电源电压|输入功率|额定功率|最大额定功率|工作室尺寸|工作区尺寸|外形尺寸|重量|重 量|"
    r"平均风速|噪音|噪 音|噪声|噪 声|照明|照 明|照度|照 度|洁净等级|空气洁净度)"
    r"\s*(?:[（(](?P<label_unit>[^）)]{1,20})[）)])?\s*(?:[:：]\s*)?"
    r"(?P<value>[^\n；;。]{1,160})",
    re.MULTILINE
)

# Regex pattern for measurement value detection
_MEASUREMENT_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)?\s*"
    r"(?:[±+−-]\s*\d+(?:[.,]\d+)?)?\s*[)）]?\s*"
    r"(?:[A-Za-z/%μµ℃°×*·+-]+(?:\s*[A-Za-z0-9/%μµ℃°×*·+-]+)?)",
    re.IGNORECASE,
)


_DETERMINISTIC_PARAMETER_RULES = (
    ("temperature_environment_and_safety", "环境温度"),
    ("temperature_environment_and_safety", "相对湿度"),
    ("temperature_environment_and_safety", "大气压力"),
    ("temperature_environment_and_safety", "控温范围"),
    ("temperature_environment_and_safety", "温度波动度"),
    ("temperature_environment_and_safety", "上偏差报警"),
    ("temperature_environment_and_safety", "定时范围"),
    ("physical_dimensions_power_and_installation", "电源电压"),
    ("physical_dimensions_power_and_installation", "输入功率"),
    ("physical_dimensions_power_and_installation", "额定功率"),
    ("physical_dimensions_power_and_installation", "最大额定功率"),
    ("physical_dimensions_power_and_installation", "工作室尺寸"),
    ("physical_dimensions_power_and_installation", "工作区尺寸"),
    ("physical_dimensions_power_and_installation", "外形尺寸"),
    ("physical_dimensions_power_and_installation", "重量"),
    ("physical_dimensions_power_and_installation", "重 量"),
    ("performance_range", "平均风速"),
    ("performance_range", "噪音"),
    ("performance_range", "噪 音"),
    ("performance_range", "噪声"),
    ("performance_range", "噪 声"),
    ("performance_range", "照明"),
    ("performance_range", "照 明"),
    ("performance_range", "照度"),
    ("performance_range", "照 度"),
    ("performance_range", "洁净等级"),
    ("performance_range", "空气洁净度"),
)

def deterministic_parameter_fields(
    documents: list[Mapping[str, Any]],
    *,
    model: str | None = None,
    other_models: list[str] | None = None,
) -> list[dict[str, Any]]:
    """从已读正文的明确参数行提取最小可比字段。

    该函数只在模型结果没有可比较参数时调用。它不推断字段、不拆解释句，
    只接受明确标签后的带单位值，并把整条原文行作为来源证据。
    """
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    evidence = source_evidence_index(documents)
    other_models = [str(item).strip() for item in (other_models or []) if str(item).strip()]
    for document in documents or []:
        document_id = str(document.get("document_id", document.get("id")) or "")
        pages = document.get("pages") if isinstance(document, Mapping) else []
        if not isinstance(pages, list):
            pages = []
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            location = str(page.get("location", page.get("source_location")) or "")
            text = str(page.get("text") or "")
            if model and other_models and any(contains_model(text, other) for other in other_models):
                # A page can carry a comparison table or two vertical product
                # blocks. Detect layout: if models appear consecutively in a small
                # region (comparison table header), treat as horizontal comparison
                # table and extract all parameter rows. Otherwise, filter by model
                # ownership.
                all_lines = text.splitlines()

                # Find the first line containing any model
                first_model_line = next((i for i, line in enumerate(all_lines)
                    if contains_model(line, model) or any(contains_model(line, other) for other in other_models)), None)

                if first_model_line is not None:
                    # Check if multiple models appear within the next 10 lines
                    # (typical for a comparison table header)
                    window_end = min(first_model_line + 10, len(all_lines))
                    models_in_window = sum(
                        1 for line in all_lines[first_model_line:window_end]
                        if contains_model(line, model) or any(contains_model(line, other) for other in other_models)
                    )

                    # If 3+ models appear in this window, likely a comparison table header
                    is_horizontal_table = models_in_window >= 3
                else:
                    is_horizontal_table = False

                if is_horizontal_table:
                    # Horizontal comparison table: all models in header, parameters below
                    # Extract all parameter lines (no model filtering needed)
                    logger.warning(f"[DEBUG] Horizontal table detected for {model}: models_in_window={models_in_window}, first_model_line={first_model_line}")
                    pass
                else:
                    # Vertical layout: filter by model ownership
                    logger.warning(f"[DEBUG] Vertical layout for {model}: first_model_line={first_model_line}")
                    selected_lines = []
                    active = False
                    for raw_line in all_lines:
                        own = contains_model(raw_line, model)
                        foreign = any(contains_model(raw_line, other) for other in other_models)
                        if own and foreign:
                            active = False
                            continue
                        if own:
                            active = True
                        elif foreign:
                            active = False
                        # Keep parameter lines (no model names) after own model activated
                        # until a foreign model appears. This handles comparison tables
                        # where parameter labels and values don't repeat the model name.
                        if active:
                            selected_lines.append(raw_line)
                    text = "\n".join(selected_lines)
            lines = text.splitlines()
            for line_idx, raw_line in enumerate(lines):
                line = raw_line.strip()
                if not line or len(line) > 240:
                    continue
                for match in _DETERMINISTIC_PARAMETER_LINE_RE.finditer(line):
                    label = match.group("label")
                    value = match.group("value").strip(" ：:，,、")
                    label_unit = str(match.group("label_unit") or "").strip() or None
                    check_value = f"{value} {label_unit or ''}"

                    # If matched value has no measurement (e.g., "外形尺寸（mm）" matches "（mm）"),
                    # check the next line for actual numeric values
                    if not _MEASUREMENT_VALUE_RE.search(check_value) and line_idx + 1 < len(lines):
                        next_line = lines[line_idx + 1].strip()
                        if next_line and len(next_line) <= 240 and _MEASUREMENT_VALUE_RE.search(next_line):
                            # Extract first value from next line (for multi-column tables, take first column)
                            value = next_line.split()[0] if next_line.split() else next_line
                            check_value = f"{value} {label_unit or ''}"
                            if not _MEASUREMENT_VALUE_RE.search(check_value):
                                continue
                        else:
                            continue
                    elif not _MEASUREMENT_VALUE_RE.search(check_value):
                        continue

                    field_name = next(
                        field for field, name in _DETERMINISTIC_PARAMETER_RULES
                        if name == label
                    )
                    key = (document_id, location, field_name, value)
                    if key in seen:
                        continue
                    seen.add(key)
                    field = {
                        "field_name": field_name,
                        "value": value,
                        "unit": label_unit,
                        "status": "pending_review",
                        "sources": [{
                            "document_id": document_id or None,
                            "page_or_location": location or None,
                            "excerpt": line,
                        }],
                    }
                    validated = validated_field(field, evidence)
                    if has_comparable_parameter(None, [validated]):
                        output.append(validated)

    # Debug logging
    if model:
        logger.warning(f"[DEBUG] _deterministic_parameter_fields for {model}: extracted {len(output)} fields")
        if output:
            logger.warning(f"[DEBUG]   Fields: {[f.get('field_name') for f in output]}")

    return output
