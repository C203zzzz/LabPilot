"""Domain-specific validation rules for temperature chambers."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from backend.services.evidence_validation.numeric_matching import numeric_tokens


_TEMPERATURE_METRICS = {
    "spatial": re.compile(
        r"(?:温度|温场)\s*均匀(?:性|度)?|temperature\s+(?:(?:spatial|spatially)\s+)?(?:uniformity|variation)",
        re.I,
    ),
    "temporal": re.compile(
        r"温度\s*(?:波动|稳定)(?:性|度)?|temperature\s+(?:(?:temporal|over\s+time)\s+)?(?:fluctuation|stability)",
        re.I,
    ),
}


def temperature_metric(label: Any) -> str | None:
    """Identify temperature metric type from label.

    Returns 'spatial' or 'temporal' if the label uniquely matches one metric type,
    otherwise None.
    """
    text = unicodedata.normalize("NFKC", str(label)).replace("_", " ")
    matches = [kind for kind, pattern in _TEMPERATURE_METRICS.items() if pattern.search(text)]
    return matches[0] if len(matches) == 1 else None


def metric_excerpt_supports(name: str, value: Any, sources: list[dict[str, Any]]) -> bool:
    """Keep easily confused chamber metrics attached to their own evidence row.

    This vocabulary applies to incubators, ovens, baths and other temperature
    chambers, independent of brand or model. Unknown labels retain the existing
    validation; this is not a general claim of semantic verification.
    """
    metric = temperature_metric(name)
    if not metric:
        return True

    required = numeric_tokens({"label": name, "value": value})
    for source in sources:
        text = unicodedata.normalize("NFKC", source["excerpt"]).replace("_", " ")
        headings = sorted(
            (m.start(), m.end(), kind)
            for kind, pattern in _TEMPERATURE_METRICS.items()
            for m in pattern.finditer(text)
        )
        for index, (start, end, kind) in enumerate(headings):
            if kind != metric:
                continue
            stop = headings[index + 1][0] if index + 1 < len(headings) else len(text)
            row = re.split(r"[;；\n]", text[start:stop], maxsplit=1)[0]
            if required and required.issubset(numeric_tokens(row)):
                return True
    return False
