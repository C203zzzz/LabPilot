"""Text normalization and manipulation utilities.

本模块是**无内部依赖**的底层工具层：任何需要"归一文本/判定型号"的模块都从这里取，
不允许各自再写一份语义不同的实现（拆分时曾复制出三份 search_text、两份
has_concrete_model，行为彼此不一致）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping


def search_text(value: Any) -> str:
    """Normalize text for search matching."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", normalized).split())


def has_concrete_model(value: Any) -> bool:
    """型号是不是一个具体型号（而不是 unknown / none 这类占位值）。"""
    normalized = search_text(value).replace(" ", "")
    return bool(normalized) and normalized not in {
        "none", "null", "unknown", "na", "notprovided",
        "未知", "无", "未提供", "不详",
    }


def scalar_texts(value: Any) -> list[str]:
    """Extract all scalar text values from nested structures."""
    if isinstance(value, Mapping):
        output: list[str] = []
        for item in value.values():
            output.extend(scalar_texts(item))
        return output
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in value:
            output.extend(scalar_texts(item))
        return output
    if value is None or isinstance(value, bool):
        return []
    text = str(value).strip()
    return [text] if text else []


def table_rows_text(table: Mapping[str, Any]) -> str:
    """Convert table rows to plain text."""
    return "\n".join(
        "\t".join(str(cell) for cell in row)
        for row in table.get("rows", []) if isinstance(row, (list, tuple))
    )


# Inventory text chunk constants
_INVENTORY_TEXT_CHUNK_CHARS = 5_000
_INVENTORY_TEXT_CHUNK_OVERLAP = 300


def text_chunks(text: Any) -> list[tuple[int, int, str]]:
    """Split text into overlapping chunks for inventory processing."""
    value = str(text or "")
    if not value:
        return [(0, 0, "")]
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + _INVENTORY_TEXT_CHUNK_CHARS)
        chunks.append((start, end, value[start:end]))
        if end == len(value):
            break
        start = max(start + 1, end - _INVENTORY_TEXT_CHUNK_OVERLAP)
    return chunks
