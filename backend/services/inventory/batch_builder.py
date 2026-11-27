"""Inventory batch construction for candidate discovery."""
from __future__ import annotations

import json
from typing import Any
from collections.abc import Mapping

from backend.infrastructure.deepseek import DeepSeekError
from backend.services.text_utils import text_chunks


# Inventory batch size limits（必须与拆分前一致：分片 5000→400 会把同一段参数表切成
# 十几块，单元归属与检索命中都会跟着变）
_INVENTORY_BATCH_CHAR_LIMIT = 24_000
_INVENTORY_TEXT_CHUNK_CHARS = 5_000
_INVENTORY_TEXT_CHUNK_OVERLAP = 300


def inventory_units(prompt_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for doc in prompt_docs:
        provenance = {
            "document_id": doc.get("document_id"),
            "source_name": doc.get("source_name"),
            "analysis_input": doc.get("analysis_input"),
        }
        for page in doc.get("pages", []):
            if not isinstance(page, Mapping):
                continue
            visual_fields = page.get("visual_fields", {})
            mentions = visual_fields.get("candidate_mentions", []) if isinstance(visual_fields, Mapping) else []
            if isinstance(mentions, Mapping):
                mentions = mentions.get("value", [])
            page_provenance = {
                **provenance,
                "analysis_input": page.get("analysis_input", provenance["analysis_input"]),
            }
            for chunk_index, (start, end, text) in enumerate(text_chunks(page.get("text", ""))):
                if not text.strip() and not mentions:
                    continue
                units.append({
                    **page_provenance,
                    "kind": "page",
                    "page": page.get("page"),
                    "location": page.get("location"),
                    "chunk_index": chunk_index,
                    "chunk_char_start": start,
                    "chunk_char_end": end,
                    "text": text,
                    "candidate_mentions": mentions if chunk_index == 0 else [],
                    "provenance": {
                        **page_provenance,
                        "page": page.get("page"),
                        "location": page.get("location"),
                    },
                })
        for table in doc.get("tables", []) if isinstance(doc.get("tables", []), list) else []:
            if not isinstance(table, Mapping):
                continue
            location = table.get("source_location") or f"table:{table.get('index', 'unknown')}"
            table_text = "\n".join(
                "\t".join(str(cell) for cell in row)
                for row in table.get("rows", []) if isinstance(row, (list, tuple))
            )
            for chunk_index, (start, end, text) in enumerate(text_chunks(table_text)):
                if not text.strip():
                    continue
                units.append({
                    **provenance,
                    "kind": "table",
                    "table_index": table.get("index"),
                    "location": location,
                    "chunk_index": chunk_index,
                    "chunk_char_start": start,
                    "chunk_char_end": end,
                    "text": text,
                    "candidate_mentions": [],
                    "provenance": {**provenance, "location": location},
                })
    return units


def split_inventory_unit(unit: dict[str, Any], maximum_encoded_characters: int) -> list[dict[str, Any]]:
    encoded = json.dumps({"evidence_units": [unit]}, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= maximum_encoded_characters:
        return [unit]
    text = str(unit.get("text", ""))
    if not text:
        raise DeepSeekError("inventory_batch_too_large", "候选清单单元元数据超过后端字符预算")
    metadata_characters = len(encoded) - len(text)
    text_limit = maximum_encoded_characters - metadata_characters - 16
    if text_limit <= 0:
        raise DeepSeekError("inventory_batch_too_large", "候选清单单元元数据超过后端字符预算")
    start_offset = int(unit.get("chunk_char_start") or 0)
    output = []
    for part_index, start in enumerate(range(0, len(text), text_limit)):
        part = text[start:start + text_limit]
        split = dict(unit)
        split["chunk_index"] = f"{unit.get('chunk_index', 0)}.{part_index}"
        split["chunk_char_start"] = start_offset + start
        split["chunk_char_end"] = start_offset + start + len(part)
        split["text"] = part
        split["candidate_mentions"] = unit.get("candidate_mentions", []) if part_index == 0 else []
        output.append(split)
    return output


def inventory_batches(prompt_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    # Reserve room for batch metadata; each unit itself is capped at 5,000 text
    # characters so this branch can always make progress.
    content_limit = _INVENTORY_BATCH_CHAR_LIMIT - 600
    for raw_unit in inventory_units(prompt_docs):
        for unit in split_inventory_unit(raw_unit, content_limit):
            candidate = current + [unit]
            encoded = json.dumps({"evidence_units": candidate}, ensure_ascii=False, separators=(",", ":"))
            if current and len(encoded) > content_limit:
                groups.append(current)
                current = [unit]
            else:
                current = candidate
    if current:
        groups.append(current)

    batches: list[dict[str, Any]] = []
    for index, units in enumerate(groups, start=1):
        payload = {
            "inventory_batch": {
                "index": index,
                "total": len(groups),
                "character_budget": _INVENTORY_BATCH_CHAR_LIMIT,
                "payload_characters": 0,
                "unit_count": len(units),
            },
            "evidence_units": units,
        }
        # The size field includes itself. Two passes are enough for its digit
        # count to stabilize, and the reserved metadata space keeps it bounded.
        for _ in range(2):
            payload["inventory_batch"]["payload_characters"] = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        if payload["inventory_batch"]["payload_characters"] > _INVENTORY_BATCH_CHAR_LIMIT:
            raise DeepSeekError("inventory_batch_too_large", "候选清单批次超过后端字符预算")
        batches.append(payload)
    return batches
