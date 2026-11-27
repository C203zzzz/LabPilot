"""Bounded per-candidate evidence payload assembly and output normalization."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.services.analysis_prompts import CANONICAL_FIELD_NAMES
from backend.services.evidence_selection import (
    allocate_page_budget,
    allocate_table_budget,
    assemble_documents,
    build_audit_info,
    candidate_terms,
    collect_evidence_units,
    select_pages,
)
from backend.services.evidence_validation import value_identity as _value_identity
from backend.services.evidence_validation.field_validator import _FIELD_SOURCE_LIMIT
from backend.services.text_utils import table_rows_text


def normalize_source(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"document_id": None, "page_or_location": None, "excerpt": ""}
    return {
        "document_id": raw.get("document_id"),
        "page_or_location": raw.get("page_or_location", raw.get("location", raw.get("page"))),
        "excerpt": str(raw.get("excerpt", ""))[:2000],
    }


def normalize_status(value: Any, *, has_source: bool) -> str:
    requested = str(value or "pending_review")
    if requested == "confirmed":
        requested = "pending_review"
    if requested not in {"pending_review", "unknown", "conflicted", "rejected"}:
        requested = "pending_review"
    return requested if has_source else "unknown"


def unparsed_field_evidence(field: Mapping[str, Any], fallback_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose source text for a value that failed structural validation."""
    raw = field.get("_raw_evidence")
    candidates = raw if isinstance(raw, list) else fallback_sources
    output = []
    seen = set()
    for source in candidates:
        if not isinstance(source, Mapping):
            continue
        excerpt = str(source.get("excerpt") or "").strip()
        if not excerpt:
            continue
        key = (source.get("document_id"), source.get("page_or_location"), excerpt)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "document_id": source.get("document_id"),
            "page_or_location": source.get("page_or_location"),
            "excerpt": excerpt[:2000],
            "evidence_status": "unparsed",
        })
        if len(output) >= _FIELD_SOURCE_LIMIT:
            break
    return output


def candidate_evidence_payload(
    candidate: Mapping[str, Any],
    prompt_docs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select a bounded, auditable page and table subset for one candidate detail call."""
    terms = candidate_terms(candidate)
    evidence_units = collect_evidence_units(prompt_docs, terms)
    all_keys = evidence_units["all_keys"]
    page_by_key = evidence_units["page_by_key"]
    page_keys_by_doc = evidence_units["page_keys_by_doc"]
    page_retrieval_profiles = evidence_units["page_retrieval_profiles"]
    table_entries = evidence_units["table_entries"]
    scores = evidence_units["scores"]

    selection_result = select_pages(candidate, prompt_docs, evidence_units)
    selected = selection_result["selected"]
    reasons = selection_result["reasons"]
    unresolved_sources = selection_result["unresolved_sources"]
    selected_before_fallback = selection_result["selected_before_fallback"]
    fallback_reason = selection_result["fallback_reason"]

    raw_sources = candidate.get("sources", [])
    selected_doc_keys_by_index = {
        doc_index: [key for key in page_keys_by_doc.get(doc_index, []) if key in selected]
        for doc_index in range(len(prompt_docs))
    }
    selected_numbers_by_doc = {
        doc_index: {
            int(page_by_key[key].get("page") or key[1] + 1)
            for key in selected_doc_keys_by_index[doc_index]
        }
        for doc_index in range(len(prompt_docs))
    }
    inventory_source_locations = {
        (str(source.get("document_id") or ""), str(source.get("page_or_location") or ""))
        for source in raw_sources
        if isinstance(source, Mapping)
    }

    table_candidates = []
    for entry in table_entries:
        doc = prompt_docs[entry["doc_index"]]
        source_key = (str(doc.get("document_id") or ""), str(entry["location"]))
        on_selected_page = entry["page"] in selected_numbers_by_doc[entry["doc_index"]]
        has_retrieval_signal = entry["profile"]["score"] > 0 or entry["candidate_score"] > 0
        if (on_selected_page or entry["page"] is None) and (
            source_key in inventory_source_locations or has_retrieval_signal
        ):
            table_candidates.append(entry)
    table_candidates.sort(key=lambda entry: (
        -int(
            (str(prompt_docs[entry["doc_index"]].get("document_id") or ""), str(entry["location"]))
            in inventory_source_locations
        ),
        -entry["candidate_score"],
        -entry["profile"]["score"],
        entry["doc_index"],
        entry["table_position"],
    ))

    (
        bounded_tables_by_doc,
        selected_table_keys,
        selected_table_text_char_count,
        truncated_table_count,
        selected_table_locations,
    ) = allocate_table_budget(
        table_candidates=table_candidates,
        selected_table_keys=selected,
        inventory_source_locations=inventory_source_locations,
        prompt_docs=prompt_docs,
        terms=terms,
    )

    selected_original_table_text_char_count = 0
    for entry in table_candidates:
        rows = [list(row) for row in entry["table"].get("rows", []) if isinstance(row, (list, tuple))]
        if rows:
            selected_original_table_text_char_count += len(table_rows_text({"rows": rows}))

    (
        selected_page_payloads_by_doc,
        selected_page_text_char_count,
        selected_original_page_text_char_count,
        truncated_page_count,
        selected_locations,
    ) = allocate_page_budget(
        all_keys=all_keys,
        selected=selected,
        page_by_key=page_by_key,
        page_retrieval_profiles=page_retrieval_profiles,
        scores=scores,
        reasons=reasons,
        prompt_docs=prompt_docs,
        terms=terms,
        selected_table_text_char_count=selected_table_text_char_count,
    )

    selected_documents = assemble_documents(
        prompt_docs=prompt_docs,
        selected_doc_keys_by_index=selected_doc_keys_by_index,
        bounded_tables_by_doc=bounded_tables_by_doc,
        selected_page_payloads_by_doc=selected_page_payloads_by_doc,
    )
    audit_info = build_audit_info(
        all_keys=all_keys,
        selected=selected,
        page_retrieval_profiles=page_retrieval_profiles,
        prompt_docs=prompt_docs,
        page_by_key=page_by_key,
        table_entries=table_entries,
        selected_table_keys=selected_table_keys,
        selected_before_fallback=selected_before_fallback,
        fallback_reason=fallback_reason,
        unresolved_sources=unresolved_sources,
        selected_page_text_char_count=selected_page_text_char_count,
        selected_original_page_text_char_count=selected_original_page_text_char_count,
        selected_table_text_char_count=selected_table_text_char_count,
        selected_original_table_text_char_count=selected_original_table_text_char_count,
        truncated_page_count=truncated_page_count,
        truncated_table_count=truncated_table_count,
        selected_locations=selected_locations,
        selected_table_locations=selected_table_locations,
    )
    audit_info["candidate_table_count"] = len(table_candidates)
    return {"evidence_selection": audit_info, "documents": selected_documents}


def normalize_analysis_output(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize raw candidate/field outputs into the canonical pending-review schema."""
    candidates = []
    for item in raw.get("candidates", []) if isinstance(raw.get("candidates", []), list) else []:
        if not isinstance(item, Mapping) or not str(item.get("model", "")).strip():
            continue
        sources = [normalize_source(x) for x in item.get("sources", [])] if isinstance(item.get("sources", []), list) else []
        sources = [s for s in sources if s["document_id"] or s["excerpt"]][:2]
        fields_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        raw_fields = item.get("fields", item.get("parameters", []))
        if isinstance(raw_fields, list):
            for field in raw_fields:
                if not isinstance(field, Mapping):
                    continue
                field_name = str(field.get("field_name", "")).strip()
                if field_name not in CANONICAL_FIELD_NAMES:
                    continue
                fs = (
                    [normalize_source(x) for x in field.get("sources", sources)]
                    if isinstance(field.get("sources", sources), list)
                    else sources
                )
                fs = [s for s in fs if s["document_id"] or s["excerpt"]][:_FIELD_SOURCE_LIMIT]
                value = field.get("value", "unknown")
                normalized_field: dict[str, Any] = {
                    "field_name": field_name,
                    "value": value,
                    "unit": field.get("unit"),
                    "status": normalize_status(field.get("status"), has_source=bool(fs)),
                    "sources": fs,
                }
                unsupported = field.get("_unsupported_parts")
                raw_value = field.get("_unsupported_values")
                raw_evidence = unparsed_field_evidence(field, fs)
                if raw_evidence and (
                    str(value).strip().casefold() in {"", "unknown", "none", "null", "未知", "未提供"}
                    or unsupported
                ):
                    normalized_field["raw_evidence"] = raw_evidence
                    normalized_field["evidence_status"] = "unparsed"
                    if isinstance(raw_value, Mapping) and raw_value:
                        normalized_field["unparsed_values"] = dict(raw_value)
                    if isinstance(unsupported, list) and unsupported:
                        normalized_field["unparsed_parts"] = [str(part) for part in unsupported]
                fields_by_name[field_name].append(normalized_field)

        fields = []
        for field_name in CANONICAL_FIELD_NAMES:
            matches = fields_by_name[field_name]
            if not matches:
                fields.append({
                    "field_name": field_name,
                    "value": "unknown",
                    "unit": None,
                    "status": "unknown",
                    "sources": [],
                })
                continue
            first = dict(matches[0])
            merged_sources = []
            source_keys = set()
            for match in matches:
                for source in match["sources"]:
                    key = (source.get("document_id"), source.get("page_or_location"), source.get("excerpt"))
                    if key not in source_keys:
                        source_keys.add(key)
                        merged_sources.append(source)
            first["sources"] = merged_sources[:_FIELD_SOURCE_LIMIT]
            raw_evidence = []
            unparsed_values = {}
            unparsed_parts = []
            for match in matches:
                for evidence in match.get("raw_evidence", []):
                    if evidence not in raw_evidence:
                        raw_evidence.append(evidence)
                if isinstance(match.get("unparsed_values"), Mapping):
                    unparsed_values.update(match["unparsed_values"])
                unparsed_parts.extend(match.get("unparsed_parts", []))
            if raw_evidence:
                first["raw_evidence"] = raw_evidence[:_FIELD_SOURCE_LIMIT]
                first["evidence_status"] = "unparsed"
            if unparsed_values:
                first["unparsed_values"] = unparsed_values
            if unparsed_parts:
                first["unparsed_parts"] = list(dict.fromkeys(unparsed_parts))
            distinct_values = {_value_identity(match["value"]) for match in matches}
            if not first["sources"]:
                first["value"] = "unknown"
                first["unit"] = None
                first["status"] = "unknown"
            elif len(distinct_values) > 1:
                first["value"] = [match["value"] for match in matches]
                first["unit"] = None if len({_value_identity(match["unit"]) for match in matches}) > 1 else first["unit"]
                first["status"] = "conflicted"
            elif any(match["status"] == "conflicted" for match in matches):
                first["status"] = "conflicted"
            else:
                supported_statuses = [match["status"] for match in matches if match["sources"]]
                first["status"] = "pending_review" if "pending_review" in supported_statuses else supported_statuses[0]
            fields.append(first)

        candidates.append({
            "candidate_id": "analysis-" + uuid.uuid4().hex[:12],
            "brand": item.get("brand"),
            "series": item.get("series"),
            "model": str(item["model"]),
            "configuration": item.get("configuration"),
            "status": "pending_review",
            "sources": sources,
            "fields": fields,
            "parameters_status": str(item.get("parameters_status") or "unknown"),
            "parameters_origin": item.get("parameters_origin"),
        })
    return {
        "analysis_id": "analysis-" + uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_review",
        "candidates": candidates,
        "disclaimer": "DeepSeek 输出仅为待复核草稿，未被确认前不得作为已核验事实或正式报价。",
    }
