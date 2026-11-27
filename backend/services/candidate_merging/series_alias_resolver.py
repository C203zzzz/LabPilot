"""Short series alias resolution and merging."""
from __future__ import annotations

import re
from typing import Any, Mapping

from backend.services.candidate_merging.configuration_merge import (
    merge_configuration as _merge_configuration,
)
from backend.services.evidence_validation import normalized_model, search_text


_INVENTORY_SOURCE_LIMIT = 8

def merge_short_series_aliases(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse an unambiguous short series label into its full candidate name.

    Catalogs sometimes use a short all-caps label in a specification-table
    heading after spelling out the full series name elsewhere. The rule is
    deliberately generic and conservative: the short label must be 3-4
    uppercase alphanumerics, be quoted in a series context, and have exactly
    one longer prefix match in the same document.
    """
    aliases: dict[int, int] = {}
    for index, candidate in enumerate(candidates):
        short = str(candidate.get("model") or "").strip()
        sources = candidate.get("sources", [])
        if not re.fullmatch(r"[A-Z0-9]{3,4}", short) or not isinstance(sources, list):
            continue
        series_sources = [
            source for source in sources
            if isinstance(source, Mapping)
            and re.search(r"(?:series|系列)", str(source.get("excerpt") or ""), re.IGNORECASE)
        ]
        if not series_sources:
            continue
        document_ids = {
            str(source.get("document_id") or "") for source in series_sources
            if str(source.get("document_id") or "")
        }
        short_key = normalized_model(short)
        matches = []
        for other_index, other in enumerate(candidates):
            if other_index == index:
                continue
            other_key = normalized_model(other.get("model"))
            if not other_key.startswith(short_key) or len(other_key) <= len(short_key):
                continue
            other_documents = {
                str(source.get("document_id") or "")
                for source in other.get("sources", [])
                if isinstance(source, Mapping) and str(source.get("document_id") or "")
            }
            if document_ids & other_documents:
                matches.append(other_index)
        if len(matches) == 1:
            aliases[index] = matches[0]

    removed: set[int] = set()
    for alias_index, target_index in aliases.items():
        if alias_index in removed or target_index in removed:
            continue
        alias = candidates[alias_index]
        target = candidates[target_index]
        for key in ("brand", "series"):
            if target.get(key) in (None, "") and alias.get(key) not in (None, ""):
                target[key] = alias.get(key)
        target["configuration"] = _merge_configuration(
            target.get("configuration"), alias.get("configuration"),
        )
        existing_sources = {
            (
                str(source.get("document_id") or ""),
                str(source.get("page_or_location") or ""),
                search_text(source.get("excerpt")),
            )
            for source in target.get("sources", []) if isinstance(source, Mapping)
        }
        for source in alias.get("sources", []):
            if not isinstance(source, Mapping):
                continue
            source_key = (
                str(source.get("document_id") or ""),
                str(source.get("page_or_location") or ""),
                search_text(source.get("excerpt")),
            )
            if source_key not in existing_sources and len(target["sources"]) < _INVENTORY_SOURCE_LIMIT:
                target["sources"].append(source)
                existing_sources.add(source_key)
        removed.add(alias_index)
    return [candidate for index, candidate in enumerate(candidates) if index not in removed]
