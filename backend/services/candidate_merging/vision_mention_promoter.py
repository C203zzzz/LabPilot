"""Vision mention promotion into inventory seeds."""
from __future__ import annotations

import re
from typing import Any, Mapping

from backend.services.candidate_service import measurement_value_not_model
from backend.services.evidence_validation import normalized_model
from backend.services.text_utils import has_concrete_model


def structured_mention_inventory(prompt_docs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Promote page-level vision mentions into auditable inventory seeds.

    Vision extraction already paid the cost of reading the rendered page. A
    later batch-level inventory call must not be allowed to silently forget a
    concrete identifier that is quoted verbatim on that page.
    """
    candidates = []
    for doc in prompt_docs:
        document_id = str(doc.get("document_id") or "")
        for page in doc.get("pages", []) if isinstance(doc.get("pages"), list) else []:
            if not isinstance(page, Mapping):
                continue
            fields = page.get("visual_fields")
            if not isinstance(fields, Mapping):
                continue
            raw_mentions = fields.get("candidate_mentions", [])
            if isinstance(raw_mentions, Mapping):
                raw_mentions = raw_mentions.get("value", [])
            if not isinstance(raw_mentions, list):
                continue
            for mention in raw_mentions:
                if not isinstance(mention, Mapping):
                    continue
                quote = str(mention.get("evidence_quote") or "").strip()
                model = mention.get("model")
                series = mention.get("series")
                identifier = model if has_concrete_model(model) else series
                if not has_concrete_model(identifier):
                    continue
                if normalized_model(identifier) not in normalized_model(quote):
                    continue
                if measurement_value_not_model(str(identifier), quote):
                    continue
                brand = mention.get("brand")
                if normalized_model(brand) in {
                    normalized_model(identifier), normalized_model(series),
                }:
                    brand = None
                candidates.append({
                    "product_role": mention.get("product_role") or "instrument",
                    "brand": brand,
                    "series": series,
                    "model": identifier,
                    "configuration": mention.get("configuration"),
                    "sources": [{
                        "document_id": document_id,
                        "page_or_location": str(page.get("location") or ""),
                        "excerpt": quote,
                    }],
                })
    for candidate in candidates:
        short = str(candidate.get("model") or "").strip()
        quote = str(candidate.get("sources", [{}])[0].get("excerpt") or "")
        if not re.fullmatch(r"[A-Z0-9]{3,4}", short) or not re.search(r"(?:series|系列)", quote, re.IGNORECASE):
            continue
        document_id = str(candidate.get("sources", [{}])[0].get("document_id") or "")
        matches = [
            other for other in candidates
            if other is not candidate
            and str(other.get("sources", [{}])[0].get("document_id") or "") == document_id
            and normalized_model(other.get("model")).startswith(normalized_model(short))
            and len(normalized_model(other.get("model"))) > len(normalized_model(short))
        ]
        if len(matches) == 1:
            candidate["model"] = matches[0].get("model")
            candidate["series"] = matches[0].get("series")
    return {"candidates": candidates}
