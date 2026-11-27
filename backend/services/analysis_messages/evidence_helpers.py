"""Evidence helper functions for excerpt and redaction."""
from typing import Any, Mapping

from backend.services.text_utils import table_rows_text


def documents_text_excerpt(documents: list[Mapping[str, Any]], limit: int = 400) -> str:
    """资料正文的一小段，只为让留痕能定位到"就是这几页没写厂名"。"""
    parts: list[str] = []
    for doc in documents:
        pages = doc.get("pages")
        if isinstance(pages, list):
            parts.extend(str(page.get("text") or "") for page in pages if isinstance(page, Mapping))
        tables = doc.get("tables")
        if isinstance(tables, list):
            parts.extend(table_rows_text(table) for table in tables if isinstance(table, Mapping))
    return "\n".join(part for part in parts if part).strip()[:limit]


def redact_text(text: Any, limit: int) -> str:
    from backend.services.procurement_research import redact_error_text

    return redact_error_text(str(text or ""))[:limit]


def owned_evidence_excerpt(owned_docs: list[Mapping[str, Any]], limit: int = 240) -> str:
    """归属到这条候选的**原文**（给排查用），按页/表拼起来。"""
    parts: list[str] = []
    for doc in owned_docs or []:
        for page in doc.get("pages") or []:
            parts.append(str(page.get("text") or ""))
        for table in doc.get("tables") or []:
            parts.append(table_rows_text(table))
    return redact_text("\n".join(part for part in parts if part).strip(), limit)


def candidate_source_excerpt(candidate: Mapping[str, Any], limit: int = 240) -> str:
    """候选自己引用的**原文**（清单环节逐字校验过的那些引文）。"""
    return redact_text(" / ".join(str(source.get("excerpt") or "")
                              for source in candidate.get("sources") or []
                              if isinstance(source, Mapping)), limit)


def failure_location() -> dict[str, Any]:
    """留痕里的出错位置：指向**调用这个助手的那一层**。

    这条消息现在住在 ``analysis_messages``，"业务 → deepseek_analysis 的转发层 →
    本模块构造器 → 本函数"比拆分前多了一层，所以取栈要深一格（``-4``），否则留痕里
    记下的会是转发层的函数名，而不是真正调用的那层业务代码。
    """
    import traceback

    frame = traceback.extract_stack()[-4]
    return {"function": frame.name, "line": frame.lineno,
            "file": frame.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]}
