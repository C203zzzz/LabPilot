"""Failure messages - technical failure records for debugging."""
import json
import re
from typing import Any, Mapping

from backend.services.analysis_messages.evidence_helpers import (
    documents_text_excerpt,
    owned_evidence_excerpt,
    candidate_source_excerpt,
    redact_text,
    failure_location,
)


def brand_unresolved_failure(models: list[str], names: list[str],
                              documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    """同一条事实的排查版：出错位置、脱敏原文、原因；与 ``run['failures']`` 同形。

    ``type`` 是这条留痕的类别（与候选发现那一侧的 ``brand_unresolved`` 同名），
    **不是抛出来的异常类**——资料里本来没有厂名，编一个异常类名等于把"资料限制"
    说成"程序故障"（``api.routes._visual_failure_records`` 对旧记录同样"如实写明
    而不是编一个"）。``error`` 与 ``excerpt`` 都先过 ``redact_error_text``：留痕
    要能定位到具体那几页，但不能把资料里的号码一起落库。
    """
    import traceback

    from backend.services.procurement_research import redact_error_text
    # 拆分前这段代码就在 deepseek_analysis 内，[-2] 指向调用它的业务代码；现在
    # 中间多了一层 deepseek_analysis 的转发函数，取栈要深一格，否则留痕里记下的
    # 是转发函数名（错误位置）。
    frame = traceback.extract_stack()[-3]
    listed = "、".join(model for model in models if model) or "未记录"
    return {
        "stage": "brand_extraction",
        "type": "BrandUnresolved",
        "code": "brand_unresolved",
        "location": {"function": frame.name, "line": frame.lineno,
                     "file": frame.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]},
        "models": [model for model in models if model],
        "error": redact_error_text(
            f"资料正文与表格里没有可用的厂名（企业名匹配到 {len(names)} 个），品牌留空；"
            f"候选型号：{listed}"),
        "excerpt": redact_error_text(documents_text_excerpt(documents))[:240],
        "note": "资料里本来没有厂名，不是异常、也不阻断分析；type 是这条留痕的类别。",
    }


def no_owned_evidence_failure(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """同一条事实的排查版：类名、出错位置、候选引用的原文。"""
    model = str(candidate.get("model") or "")
    return {
        "stage": "parameters",
        "type": "ModelWithoutOwnedEvidence",
        "code": "no_owned_evidence",
        "location": failure_location(),
        "models": [model] if model else [],
        "error": redact_text(
            f"候选 {model} 在本轮资料里关联不到任何一页正文（scoped_documents 为空），"
            "参数抽取没有发起；参数留空是「未取到」，不等于资料里没有这台机器的参数。", 400),
        "excerpt": candidate_source_excerpt(candidate),
        "note": "判据没放宽：只是这条候选自己的证据范围为空，不是型号匹配被改宽了。",
    }


def parameter_fallback_failure(model: Any, raw_response: Any,
                                recovered_by: str | None = None) -> dict[str, Any]:
    """保留二次模型返回，供排查使用，同时先做脱敏和长度限制。"""
    try:
        serialized = json.dumps(raw_response, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = repr(raw_response)
    # ``redact_error_text`` handles assignment-style diagnostics. Model JSON
    # commonly uses quoted keys, so cover credential-shaped JSON values before
    # handing the response to the shared redactor.
    serialized = re.sub(
        r'(?i)(["\'](?:api[-_]?key|password|passwd|authorization|token|secret)["\']\s*:\s*)(["\'])(.*?)(\2)',
        lambda match: f"{match.group(1)}{match.group(2)}***{match.group(4)}",
        serialized,
    )
    redacted = redact_text(serialized, 3500)
    return {
        "stage": "parameters",
        "type": "ModelReturnedMissingStructuredConfiguration",
        "code": "parameter_fallback_empty",
        "location": failure_location(),
        "models": [str(model or "")] if model else [],
        "error": redact_text(
            "二次参数抽取的原始返回缺少结构化配置或可比参数：" + serialized,
            4000,
        ),
        "raw_response": redacted,
        "raw_response_type": type(raw_response).__name__,
        "note": (
            "已读取正文仍保留在候选证据中；没有接受未被原文校验的参数。"
            + (f" 已由 {recovered_by} 恢复可比较参数。" if recovered_by else "")
        ),
        **({"recovered_by": recovered_by} if recovered_by else {}),
    }


def parameter_extraction_incomplete_failure(model: Any, exc: Any,
                                             documents: list[Mapping[str, Any]],
                                             recovered_by: str | None = None) -> dict[str, Any]:
    return {
        "stage": "parameters",
        "type": "ParameterExtractionIncomplete",
        "code": "parameter_extraction_incomplete",
        "location": failure_location(),
        "models": [str(model or "")] if model else [],
        "error": redact_text(f"{type(exc).__name__}: {getattr(exc, 'message', exc)}", 400),
        "excerpt": documents_text_excerpt(documents),
        "note": (
            "参数留空是因为抽取被中断（未取到），不代表资料里没有参数。"
            + (f" 已由 {recovered_by} 恢复可比较参数。" if recovered_by else "")
        ),
        **({"recovered_by": recovered_by} if recovered_by else {}),
    }


def sibling_merge_failure(candidate: Mapping[str, Any], sibling: Mapping[str, Any],
                           number: str, sibling_documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    model = str(candidate.get("model") or "")
    sibling_model = str(sibling.get("model") or "")
    return {
        "stage": "parameters",
        "type": "SiblingCandidateMerge",
        "code": "sibling_candidate_merge",
        "location": failure_location(),
        "models": [model, sibling_model],
        "number": number,
        "merged_from": sibling_model,
        "error": redact_text(
            f"候选 {model} 在本轮资料里关联不到任何一页正文，同编号（{number}）的候选 "
            f"{sibling_model} 有归属页，参数按同编号归并取自 {sibling_model}；"
            f"本候选自己的引文：{candidate_source_excerpt(candidate, 120)}", 400),
        "excerpt": owned_evidence_excerpt(sibling_documents),
        "note": "型号匹配判据没有放宽；归并只在'这条候选一个字都关联不到'且编号逐字相同时发生。",
    }
