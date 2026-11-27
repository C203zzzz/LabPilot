"""Text normalization utilities for evidence validation."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# 文本归一只能有一份实现：``text_utils`` 里的版本与拆分前 deepseek_analysis 的
# ``_search_text`` / ``_scalar_texts`` 逐字一致。这里只做转出，不再自造第二份语义
# 不同的实现——自造版本曾让 candidate_merging 与主流程用上两套"归一文本"。
from backend.services.text_utils import scalar_texts, search_text

__all__ = ["normalized_model", "scalar_texts", "search_text", "value_identity"]


def normalized_model(value: Any) -> str:
    """Normalize model identifier by removing non-alphanumeric characters.

    与拆分前 deepseek_analysis 的 ``_normalized_model`` 逐字一致：``value or ""``
    把 0 / False 这类假值也当作空串，不能写成 ``str(value)``。
    """
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def value_identity(value: Any) -> str:
    """值的身份串：结构不同就必须不同。

    与拆分前 deepseek_analysis 的 ``_value_identity`` 逐字一致（``json.dumps`` 版）。
    拆分时这里曾写成"把 dict 的 value 用空格拼起来"，于是
    ``{"a": "12"}`` 与 ``{"a": "1", "b": "2"}`` 被判成同一身份——值修复时会把不同的
    参数值当成"同一个值的多种写法"合并掉。
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)
