"""M11 deterministic report assembly, export, and version orchestration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from backend.domain.errors import ModuleValidationError
from backend.domain.reports import (
    EvidenceReference, ExportFormat, ExportResult, InMemoryReportRepository,
    ReportItem, ReportRepository, ReportSection, ReportSnapshot, ReportVersion,
    SelectionReport, SelectionStatus,
)
from backend.services.question_service import generate_question_list
from backend.services.recommendation_service import rank_candidates

_EVALUATION_STATUSES = {"eligible", "needs_review", "ineligible"}
_RECOMMENDATION_CATEGORIES = {"preferred", "alternative", "needs_review", "ineligible"}
_CATEGORY_STATUS = {
    "preferred": "eligible", "alternative": "eligible",
    "needs_review": "needs_review", "ineligible": "ineligible",
}


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _primitive(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _primitive(value.value)
    if is_dataclass(value):
        return _primitive(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(item) for item in value]
    return value


def _as_dict(value: Any, field_name: str) -> dict[str, Any]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        data = value.to_dict()
    elif is_dataclass(value):
        data = asdict(value)
    elif isinstance(value, Mapping):
        data = dict(value)
    else:
        raise ModuleValidationError(f"{field_name} must be an object", code="invalid_input", field=field_name)
    data = _primitive(data)
    if not isinstance(data, dict):
        raise ModuleValidationError(f"{field_name} must be an object", code="invalid_input", field=field_name)
    return data


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModuleValidationError(f"{field_name} is required", code="invalid_input", field=field_name)
    return value.strip()


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ModuleValidationError(f"{field_name} must be a list or tuple", code="invalid_input", field=field_name)
    return list(value)


def _canonical(value: Any) -> str:
    return json.dumps(_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalise_requirement(requirement: Any) -> dict[str, Any]:
    data = _as_dict(requirement, "requirement")
    data["scenario"] = _required_text(data.get("scenario"), "scenario")
    for key in ("hard_constraints", "soft_constraints", "capabilities", "unknowns"):
        if data.get(key) is None:
            data[key] = []
        data[key] = _list(data[key], f"requirement.{key}")
    return data


def _normalise_candidates(candidates: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _list(candidates, "candidates"):
        data = _as_dict(raw, "candidate")
        candidate_id = _required_text(data.get("id") or data.get("candidate_id"), "candidate.id")
        if candidate_id in seen:
            raise ModuleValidationError("duplicate candidate_id", code="invalid_input", field="candidate_id")
        seen.add(candidate_id)
        data["id"] = candidate_id
        data["candidate_id"] = candidate_id
        data["model"] = _required_text(data.get("model"), "candidate.model")
        data["sources"] = [_as_dict(source, "candidate.sources") for source in _list(data.get("sources", []), "candidate.sources")]
        result.append(data)
    return sorted(result, key=lambda item: item["id"])


def _normalise_evaluations(evaluations: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _list(evaluations, "evaluations"):
        data = _as_dict(raw, "evaluation")
        candidate_id = _required_text(data.get("candidate_id"), "evaluation.candidate_id")
        if candidate_id in seen:
            raise ModuleValidationError("duplicate evaluation candidate_id", code="invalid_input", field="candidate_id")
        seen.add(candidate_id)
        status = str(_value(data.get("status", "")))
        if status not in _EVALUATION_STATUSES:
            raise ModuleValidationError("invalid evaluation status", code="invalid_input", field="status")
        data["status"] = status
        data["eligible"] = bool(data.get("eligible", status == "eligible"))
        if data["eligible"] != (status == "eligible"):
            raise ModuleValidationError("eligible must match evaluation status", code="invalid_evaluation", field="eligible")
        for key in ("hard_constraints", "soft_constraints"):
            data[key] = _list(data.get(key, []), f"evaluation.{key}")
        result.append(data)
    return sorted(result, key=lambda item: item["candidate_id"])


def _normalise_facts(facts: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _list(facts, "facts"):
        data = _as_dict(raw, "fact")
        fact_id = _required_text(data.get("fact_id"), "fact.fact_id")
        if fact_id in seen:
            raise ModuleValidationError("duplicate fact_id", code="invalid_input", field="fact_id")
        seen.add(fact_id)
        data["candidate_id"] = _required_text(data.get("candidate_id"), "fact.candidate_id")
        data["field_name"] = _required_text(data.get("field_name"), "fact.field_name")
        if data.get("value") is None:
            raise ModuleValidationError("fact.value is required", code="invalid_input", field="value")
        data["evidence"] = [_as_dict(evidence, "fact.evidence") for evidence in _list(data.get("evidence", []), "fact.evidence")]
        result.append(data)
    return sorted(result, key=lambda item: item["fact_id"])


def _normalise_quotes(quotes: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _list(quotes, "quotes"):
        data = _as_dict(raw, "quote")
        quote_id = _required_text(data.get("quote_id"), "quote.quote_id")
        if quote_id in seen:
            raise ModuleValidationError("duplicate quote_id", code="invalid_input", field="quote_id")
        seen.add(quote_id)
        data["candidate_id"] = _required_text(data.get("candidate_id"), "quote.candidate_id")
        data["conditions"] = [_as_dict(condition, "quote.conditions") for condition in _list(data.get("conditions", []), "quote.conditions")]
        result.append(data)
    return sorted(result, key=lambda item: item["quote_id"])


def _normalise_comparisons(comparisons: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in _list(comparisons, "comparisons"):
        data = _as_dict(raw, "comparison")
        data["candidate_id"] = _required_text(data.get("candidate_id"), "comparison.candidate_id")
        for key in ("comparable", "incomparable", "gaps"):
            data[key] = _list(data.get(key, []), f"comparison.{key}")
        result.append(data)
    return sorted(result, key=lambda item: item["candidate_id"])


def _normalise_questions(questions: Any) -> dict[str, Any] | None:
    if questions is None:
        return None
    if hasattr(questions, "to_dict") and callable(questions.to_dict):
        data = _as_dict(questions, "questions")
    elif isinstance(questions, Mapping):
        data = _as_dict(questions, "questions")
    elif isinstance(questions, (list, tuple)):
        data = {"questions": [_as_dict(item, "questions.item") for item in questions], "rule_version": "m10-v1"}
    else:
        raise ModuleValidationError("questions must be QuestionList, mapping, or list", code="invalid_input", field="questions")
    raw_questions = [_as_dict(item, "questions.item") for item in _list(data.get("questions", []), "questions.questions")]
    raw_questions.sort(key=lambda item: (str(item.get("candidate_id", "")), str(item.get("question_id", ""))))
    groups: dict[str, list[dict[str, Any]]] = {}
    for question in raw_questions:
        candidate_id = question.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            nested = question.get("evidence")
            candidate_id = nested.get("candidate_id") if isinstance(nested, Mapping) else None
        candidate_id = _required_text(candidate_id, "question.candidate_id")
        question["candidate_id"] = candidate_id
        groups.setdefault(candidate_id, []).append(question)
    return {"questions": raw_questions, "candidate_groups": {key: groups[key] for key in sorted(groups)}, "rule_version": str(data.get("rule_version", "m10-v1"))}

def _normalise_ranking(ranking: Any, evaluations: list[dict[str, Any]], facts: list[dict[str, Any]]) -> dict[str, Any]:
    data = _as_dict(rank_candidates(evaluations, facts), "ranking") if ranking is None else _as_dict(ranking, "ranking")
    status = str(_value(data.get("status", "")))
    if status not in {SelectionStatus.RECOMMENDATIONS.value, SelectionStatus.NO_RECOMMENDATION.value}:
        raise ModuleValidationError("invalid ranking status", code="invalid_ranking", field="status")
    for key in ("alternatives", "needs_review", "ineligible"):
        data[key] = _list(data.get(key, []), f"ranking.{key}")
    preferred = data.get("preferred")
    if preferred is not None and not isinstance(preferred, Mapping):
        raise ModuleValidationError("ranking.preferred must be an object or null", code="invalid_ranking", field="preferred")
    data["preferred"] = dict(preferred) if preferred is not None else None
    return _primitive(data)


def _missing_candidate(candidate_id: str, field_name: str) -> None:
    raise ModuleValidationError(f"candidate reference not found: {candidate_id}", code="candidate_reference_missing", field=field_name)


def _validate_candidate_references(candidates, evaluations, facts, quotes, comparisons, questions, ranking) -> None:
    candidate_ids = {candidate["id"] for candidate in candidates}
    for collection, field_name in ((evaluations, "evaluations"), (facts, "facts"), (quotes, "quotes"), (comparisons, "comparisons")):
        for item in collection:
            if str(item.get("candidate_id", "")) not in candidate_ids:
                _missing_candidate(str(item.get("candidate_id", "")), field_name)
    if questions is not None:
        for question in questions["questions"]:
            candidate_id = str(question.get("candidate_id", ""))
            if candidate_id not in candidate_ids:
                _missing_candidate(candidate_id, "questions")
            evidence = question.get("evidence")
            if isinstance(evidence, Mapping) and evidence.get("candidate_id") not in (None, candidate_id):
                raise ModuleValidationError("question evidence candidate_id mismatch", code="association_mismatch", field="questions")
    recommendation_items = ([ranking["preferred"]] if ranking.get("preferred") else []) + list(ranking["alternatives"]) + list(ranking["needs_review"]) + list(ranking["ineligible"])
    for item in recommendation_items:
        candidate_id = str(_get(item, "candidate_id", ""))
        if candidate_id not in candidate_ids:
            _missing_candidate(candidate_id, "ranking")


def _validate_ranking_safety(ranking: dict[str, Any], evaluations: list[dict[str, Any]]) -> None:
    status_by_candidate = {item["candidate_id"]: item["status"] for item in evaluations}
    status = ranking["status"]
    if status == SelectionStatus.RECOMMENDATIONS.value and ranking.get("preferred") is None:
        raise ModuleValidationError("recommendations status requires a preferred candidate", code="invalid_recommendation", field="preferred")
    if status == SelectionStatus.NO_RECOMMENDATION.value and ranking.get("preferred") is not None:
        raise ModuleValidationError("no_recommendation status cannot include a preferred candidate", code="invalid_recommendation", field="preferred")

    def check(item, expected_category: str, field_name: str) -> None:
        if not isinstance(item, Mapping):
            raise ModuleValidationError("recommendation must be an object", code="invalid_recommendation", field=field_name)
        candidate_id = str(item.get("candidate_id", ""))
        category = str(_value(item.get("category", "")))
        if category != expected_category or category not in _RECOMMENDATION_CATEGORIES:
            raise ModuleValidationError("recommendation category does not match its ranking bucket", code="invalid_recommendation", field=field_name)
        if candidate_id not in status_by_candidate:
            _missing_candidate(candidate_id, "ranking")
        expected_status = _CATEGORY_STATUS[category]
        if status_by_candidate[candidate_id] != expected_status:
            if category in {"preferred", "alternative"}:
                raise ModuleValidationError("needs_review and ineligible candidates cannot be preferred or alternative", code="invalid_recommendation", field=field_name)
            raise ModuleValidationError("recommendation category and evaluation status disagree", code="invalid_recommendation", field=field_name)

    if ranking.get("preferred") is not None:
        check(ranking["preferred"], "preferred", "preferred")
    for item in ranking["alternatives"]:
        check(item, "alternative", "alternatives")
    for item in ranking["needs_review"]:
        check(item, "needs_review", "needs_review")
    for item in ranking["ineligible"]:
        check(item, "ineligible", "ineligible")


def _candidate_label(candidate: Mapping[str, Any] | None) -> str:
    if not candidate:
        return "未知候选"
    pieces = [candidate.get("brand"), candidate.get("series"), candidate.get("model"), candidate.get("configuration")]
    return " ".join(str(piece).strip() for piece in pieces if piece and str(piece).strip()) or str(candidate.get("id", "未知候选"))


def _quoted(value: Any) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _ref_from_evidence(evidence: Mapping[str, Any], fact_id: str | None, candidate_id: str | None) -> EvidenceReference:
    document_id = evidence.get("document_id")
    page = evidence.get("page")
    location = evidence.get("location")
    url = evidence.get("url")
    body_location = evidence.get("body_location")
    missing_reason = evidence.get("missing_reason")
    if not missing_reason and not ((document_id and (location or page is not None)) or (url and (body_location or location))):
        missing_reason = "上游证据未提供 document_id/page 或 URL/body 定位"
    return EvidenceReference(
        fact_id=fact_id, evidence_id=evidence.get("evidence_id"), candidate_id=candidate_id,
        document_id=document_id, page=page, location=location, url=url, body_location=body_location,
        quote=evidence.get("quote"), source_type=evidence.get("source_type"),
        price_evidence_level=evidence.get("price_evidence_level"), tender_title=evidence.get("tender_title"),
        publication_date=evidence.get("publication_date"), currency=evidence.get("currency"),
        tax_condition=evidence.get("tax_condition"), price_amount=evidence.get("price_amount"),
        price_unit=evidence.get("price_unit"), price_quote=evidence.get("price_quote"), missing_reason=missing_reason,
    )


def _refs_from_ids(fact_ids, evidence_ids, candidate_id: str, facts_by_id, evidence_by_id) -> tuple[EvidenceReference, ...]:
    refs: list[EvidenceReference] = []
    seen: set[tuple[Any, Any]] = set()
    for raw_fact_id in sorted({str(value) for value in (fact_ids or []) if str(value).strip()}):
        fact = facts_by_id.get(raw_fact_id)
        if fact is None:
            reference = EvidenceReference(fact_id=raw_fact_id, candidate_id=candidate_id, missing_reason="fact_id 未在报告输入的事实索引中找到")
            refs.append(reference)
            seen.add((reference.fact_id, reference.evidence_id))
            continue
        evidence_items = list(fact.get("evidence", []) or [])
        if not evidence_items:
            reference = EvidenceReference(fact_id=raw_fact_id, candidate_id=candidate_id, missing_reason="该事实没有关联证据")
            refs.append(reference)
            seen.add((reference.fact_id, reference.evidence_id))
        for evidence in evidence_items:
            reference = _ref_from_evidence(evidence, raw_fact_id, str(evidence.get("candidate_id") or candidate_id))
            key = (reference.fact_id, reference.evidence_id)
            if key not in seen:
                refs.append(reference)
                seen.add(key)
    for raw_evidence_id in sorted({str(value) for value in (evidence_ids or []) if str(value).strip()}):
        evidence = evidence_by_id.get(raw_evidence_id)
        reference = _ref_from_evidence(evidence, evidence.get("fact_id"), str(evidence.get("candidate_id") or candidate_id)) if evidence else EvidenceReference(evidence_id=raw_evidence_id, candidate_id=candidate_id, missing_reason="evidence_id 未在报告输入的证据索引中找到")
        key = (reference.fact_id, reference.evidence_id)
        if key not in seen:
            refs.append(reference)
            seen.add(key)
    return tuple(refs)


def _reason_view(reason: Mapping[str, Any], candidate_id: str, facts_by_id, evidence_by_id) -> dict[str, Any]:
    fact_ids = [str(value) for value in reason.get("fact_ids", []) or []]
    evidence_ids = [str(value) for value in reason.get("evidence_ids", []) or []]
    refs = _refs_from_ids(fact_ids, evidence_ids, candidate_id, facts_by_id, evidence_by_id)
    missing_reason = reason.get("missing_reason")
    if not refs and not missing_reason:
        missing_reason = "上游结论未提供 fact_id/evidence_id"
    return {"text": str(reason.get("text", "")), "kind": str(reason.get("kind", "positive")),
            "fact_ids": sorted(set(fact_ids)), "evidence_ids": sorted(set(evidence_ids)),
            "evidence": [reference.to_dict() for reference in refs], "missing_reason": missing_reason}


def _recommendation_view(recommendation: Mapping[str, Any], facts_by_id, evidence_by_id) -> dict[str, Any]:
    candidate_id = str(recommendation.get("candidate_id", ""))
    dimensions: list[dict[str, Any]] = []
    for raw in recommendation.get("dimensions", []) or []:
        dimension = dict(raw)
        fact_ids = [str(value) for value in dimension.get("fact_ids", []) or []]
        evidence_ids = [str(value) for value in dimension.get("evidence_ids", []) or []]
        refs = _refs_from_ids(fact_ids, evidence_ids, candidate_id, facts_by_id, evidence_by_id)
        dimension["evidence"] = [reference.to_dict() for reference in refs]
        if not refs and not dimension.get("missing_reason"):
            dimension["missing_reason"] = "评分维度未提供 fact_id/evidence_id"
        dimensions.append(dimension)
    result = {"candidate_id": candidate_id, "category": str(_value(recommendation.get("category", ""))),
              "rank": recommendation.get("rank"), "total_score": recommendation.get("total_score"),
              "dimensions": dimensions,
              "reasons": [_reason_view(reason, candidate_id, facts_by_id, evidence_by_id) for reason in recommendation.get("reasons", []) or []],
              "risks": [_reason_view(reason, candidate_id, facts_by_id, evidence_by_id) for reason in recommendation.get("risks", []) or []],
              "gaps": [_reason_view(reason, candidate_id, facts_by_id, evidence_by_id) for reason in recommendation.get("gaps", []) or []]}
    result["evidence"] = _unique_reference_dicts(result["reasons"] + result["risks"] + result["gaps"] + dimensions)
    return result


def _unique_reference_dicts(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for group in groups:
        for reference in group.get("evidence", []) or []:
            unique[_canonical(reference)] = reference
    return [unique[key] for key in sorted(unique)]


def _refs_from_view(view: Mapping[str, Any]) -> tuple[EvidenceReference, ...]:
    return tuple(EvidenceReference(**reference) for reference in view.get("evidence", []) or [])


def _item(label: str, text: str, candidate_id: str | None, refs: tuple[EvidenceReference, ...], fallback_reason: str) -> ReportItem:
    return ReportItem(label=label, text=text, candidate_id=candidate_id, evidence=refs, missing_reason=None if refs else fallback_reason)

def _decision_text(view: Mapping[str, Any], candidate: Mapping[str, Any] | None) -> str:
    name = _candidate_label(candidate)
    score = view.get("total_score")
    suffix = f"，总分 {score}" if score is not None else ""
    reasons = [item.get("text", "") for item in view.get("reasons", []) if item.get("text")]
    if not reasons:
        reasons = [item.get("text", "") for item in view.get("risks", []) if item.get("text")]
    return f"{view['candidate_id']}（{name}）{suffix}。理由：{'；'.join(reasons) if reasons else '未提供结构化理由'}"


def _recommendation_section(report_status, preferred, alternatives, needs_review, ineligible, candidates_by_id) -> ReportSection:
    items: list[ReportItem] = []
    if preferred is None:
        refs = tuple(reference for item in ineligible for reference in _refs_from_view(item))
        items.append(ReportItem(label="无推荐", text="无推荐（no_recommendation）：没有候选满足全部硬约束并处于 eligible 状态。",
                                evidence=refs, missing_reason=None if refs else "该结论来自 M07/M08 状态；没有可引用的单一 fact_id/evidence_id"))
        summary = "无推荐：没有 eligible 候选满足全部硬约束。"
    else:
        refs = _refs_from_view(preferred)
        items.append(_item("首选", _decision_text(preferred, candidates_by_id.get(preferred["candidate_id"])), preferred["candidate_id"], refs, "M08 首选理由未提供 fact_id/evidence_id"))
        summary = f"首选：{preferred['candidate_id']}；备选 {len(alternatives)} 个。"
    for alternative in alternatives:
        refs = _refs_from_view(alternative)
        items.append(_item("备选", _decision_text(alternative, candidates_by_id.get(alternative["candidate_id"])), alternative["candidate_id"], refs, "M08 备选理由未提供 fact_id/evidence_id"))
    for review in needs_review:
        refs = _refs_from_view(review)
        items.append(_item("待复核（不得作为首选）", _decision_text(review, candidates_by_id.get(review["candidate_id"])), review["candidate_id"], refs, "M08 待复核原因未提供 fact_id/evidence_id"))
    for bad in ineligible:
        refs = _refs_from_view(bad)
        items.append(_item("淘汰原因", _decision_text(bad, candidates_by_id.get(bad["candidate_id"])), bad["candidate_id"], refs, "M08 淘汰原因未提供 fact_id/evidence_id"))
    evidence = tuple(reference for item in items for reference in item.evidence)
    return ReportSection("recommendation", "首选、备选、待复核与淘汰原因", summary, tuple(items), evidence)


def _requirement_section(requirement: Mapping[str, Any]) -> ReportSection:
    evidence_gap = "需求来自用户输入；M01 需求契约的约束项没有独立 fact_id/evidence_id"
    items = [ReportItem("采购场景", str(requirement["scenario"]), missing_reason=evidence_gap)]
    for label, key in (("任务类型", "task_type"), ("预算", "budget"), ("通量", "throughput"), ("环境", "environment"), ("能力要求", "capabilities")):
        value = requirement.get(key)
        if value not in (None, [], {}):
            items.append(ReportItem(label, _quoted(value), missing_reason=evidence_gap))
    for kind, key in (("硬约束", "hard_constraints"), ("软约束", "soft_constraints")):
        for constraint in requirement.get(key, []) or []:
            text = f"{constraint.get('name', '未命名约束')} = {_quoted(constraint.get('value'))}"
            if constraint.get("source_text"):
                text += f"；原文：{constraint['source_text']}"
            items.append(ReportItem(kind, text, missing_reason=evidence_gap))
    for unknown in requirement.get("unknowns", []) or []:
        items.append(ReportItem("未知项", str(unknown), missing_reason="需求尚未形成可引用事实或证据"))
    return ReportSection("requirements", "采购需求", f"采购场景：{requirement['scenario']}", tuple(items))


def _candidate_section(candidates: list[dict[str, Any]]) -> ReportSection:
    items: list[ReportItem] = []
    for candidate in candidates:
        text = f"{candidate['id']}：{_candidate_label(candidate)}；状态：{candidate.get('status', 'unknown')}"
        refs: list[EvidenceReference] = []
        reasons: list[str] = []
        for source in candidate.get("sources", []) or []:
            document_id, location, url = source.get("document_id"), source.get("location"), source.get("url")
            if document_id and location:
                refs.append(EvidenceReference(candidate_id=candidate["id"], document_id=document_id, location=location, quote=source.get("excerpt"), source_type=source.get("source_type")))
            elif url and location:
                refs.append(EvidenceReference(candidate_id=candidate["id"], url=url, body_location=location, quote=source.get("excerpt"), source_type=source.get("source_type")))
            else:
                reasons.append("候选来源缺少 document_id/page 或 URL/body 定位")
        items.append(ReportItem("候选", text, candidate["id"], tuple(refs), None if refs else "；".join(reasons) or "候选没有来源定位"))
    if not items:
        items.append(ReportItem("候选", "没有候选输入", missing_reason="候选列表为空"))
    return ReportSection("candidates", "候选产品", f"共 {len(candidates)} 个候选", tuple(items))


def _hard_constraint_section(evaluations, candidates_by_id, facts_by_id, evidence_by_id) -> ReportSection:
    items: list[ReportItem] = []
    for evaluation in evaluations:
        candidate_id = evaluation["candidate_id"]
        constraints = evaluation.get("hard_constraints", []) or []
        if not constraints:
            items.append(ReportItem("硬约束", f"{candidate_id}：没有硬约束判断", candidate_id, missing_reason="M07 输出没有硬约束判断项"))
        for constraint in constraints:
            refs = _refs_from_ids(constraint.get("fact_ids", []), constraint.get("evidence_ids", []), candidate_id, facts_by_id, evidence_by_id)
            text = f"{candidate_id}：{constraint.get('constraint_name', '未命名')} 为 {constraint.get('status', 'unknown')}"
            if constraint.get("reason"):
                text += f"；{constraint['reason']}"
            items.append(_item("硬约束", text, candidate_id, refs, str(constraint.get("missing_reason") or "硬约束判断未提供 fact_id/evidence_id 或缺失原因")))
    if not items:
        items.append(ReportItem("硬约束", "没有结构化硬约束判断", missing_reason="M07 评估列表为空"))
    return ReportSection("hard_constraints", "硬约束判断", f"覆盖 {len(evaluations)} 个候选评价", tuple(items))


def _fact_section(facts, facts_by_id, evidence_by_id) -> ReportSection:
    items: list[ReportItem] = []
    for fact in facts:
        refs = _refs_from_ids([fact["fact_id"]], [], fact["candidate_id"], facts_by_id, evidence_by_id)
        historical = any(evidence.get("price_evidence_level") == "historical_tender" for evidence in fact.get("evidence", []) or [])
        label = "历史参考事实（非当前报价）" if historical else "事实"
        text = f"{fact['candidate_id']} / {fact['field_name']} = {_quoted(fact.get('value'))}"
        if fact.get("unit"):
            text += f" {fact['unit']}"
        text += f"；状态：{fact.get('status', 'unknown')}"
        if fact.get("note"):
            text += f"；备注：{fact['note']}"
        if historical:
            text += "；该价格来自历史招标，仅作历史参考，不能视为当前报价"
        items.append(_item(label, text, fact["candidate_id"], refs, "事实没有关联证据"))
    if not items:
        items.append(ReportItem("事实", "没有结构化事实输入", missing_reason="事实列表为空"))
    return ReportSection("facts", "事实与证据", f"共 {len(facts)} 条事实", tuple(items))


def _quote_label(source_type: str) -> str:
    return {"current_supplier_quote": "当前供应商报价", "official_public_price": "官方公开价（非供应商当前正式报价）",
            "historical_tender_price": "历史招标价（历史参考，非当前报价）", "dealer_lead": "经销商线索（非正式报价）",
            "unknown": "来源未知价格（不可作为正式报价）"}.get(source_type, "报价/价格证据")


def _quote_ref(quote: Mapping[str, Any]) -> EvidenceReference:
    source, location = quote.get("source"), quote.get("source_location")
    source_type = str(_value(quote.get("source_type", "unknown")))
    document_id = source if source and not (isinstance(source, str) and re.match(r"^https?://", source, flags=re.I)) else None
    url = source if isinstance(source, str) and re.match(r"^https?://", source, flags=re.I) else None
    missing_reason = None if location else "报价来源只有 source，缺少 page/body 定位"
    return EvidenceReference(quote_id=str(quote["quote_id"]), candidate_id=str(quote["candidate_id"]),
                             document_id=str(document_id) if document_id else None,
                             location=str(location) if location else None, url=url,
                             body_location=str(location) if url and location else None,
                             source_type=source_type, missing_reason=missing_reason)


def _quote_section(quotes) -> ReportSection:
    items: list[ReportItem] = []
    for quote in quotes:
        source_type = str(_value(quote.get("source_type", "unknown")))
        label = _quote_label(source_type)
        text = f"{label}：{quote['candidate_id']} / {quote.get('supplier', '未知供应商')} / {quote.get('model', '未知型号')}；金额 {quote.get('amount', 'unknown')} {quote.get('currency', 'unknown')}"
        if quote.get("quote_date"):
            text += f"；报价日期 {quote['quote_date']}"
        if quote.get("valid_until"):
            text += f"；有效期至 {quote['valid_until']}"
        known_conditions = [f"{condition.get('condition_type')}={_quoted(condition.get('value'))}" for condition in quote.get("conditions", []) or [] if condition.get("value") not in (None, "", "unknown")]
        if known_conditions:
            text += "；条件：" + "；".join(known_conditions)
        if source_type == "historical_tender_price":
            text += "。该金额只能作为历史参考，不得表述为当前报价或当前可比价格"
        if quote.get("note"):
            text += f"；备注：{quote['note']}"
        items.append(_item(label, text, quote["candidate_id"], (_quote_ref(quote),), "报价缺少可定位来源"))
    if not items:
        items.append(ReportItem("报价", "没有结构化报价输入", missing_reason="报价列表为空"))
    return ReportSection("quotes", "报价与采购条件", f"共 {len(quotes)} 条报价或价格记录", tuple(items))


def _recommendation_risk_gap_items(recommendation_views, key, facts_by_id, evidence_by_id) -> list[ReportItem]:
    items: list[ReportItem] = []
    for view in recommendation_views:
        for raw in view.get(key, []) or []:
            refs = _refs_from_ids(raw.get("fact_ids", []), raw.get("evidence_ids", []), view["candidate_id"], facts_by_id, evidence_by_id)
            items.append(_item("风险" if key == "risks" else "缺口", f"{view['candidate_id']}：{raw.get('text', '')}", view["candidate_id"], refs, str(raw.get("missing_reason") or "上游未提供 fact_id/evidence_id")))
    return items


def _risk_section(recommendation_views, facts_by_id, evidence_by_id) -> ReportSection:
    items = _recommendation_risk_gap_items(recommendation_views, "risks", facts_by_id, evidence_by_id)
    if not items:
        items = [ReportItem("风险", "没有结构化风险记录；这不表示不存在风险", missing_reason="M08 未提供结构化风险项")]
    return ReportSection("risks", "风险", f"共 {len(items)} 条风险记录", tuple(items))


def _gap_section(recommendation_views, requirement, comparisons, facts_by_id, evidence_by_id) -> ReportSection:
    items = _recommendation_risk_gap_items(recommendation_views, "gaps", facts_by_id, evidence_by_id)
    for unknown in requirement.get("unknowns", []) or []:
        items.append(ReportItem("需求缺口", str(unknown), missing_reason="需求未知项尚无可引用事实或证据"))
    for comparison in comparisons:
        candidate_id = comparison["candidate_id"]
        quote_ids = [str(quote.get("quote_id")) for quote in comparison.get("quotes", []) or []]
        for gap in comparison.get("gaps", []) or []:
            refs = tuple(EvidenceReference(quote_id=quote_id, candidate_id=candidate_id, missing_reason="仅保留 quote_id，详细报价证据未包含在 comparison 中") for quote_id in quote_ids)
            items.append(_item("报价缺口", f"{candidate_id}：{gap}", candidate_id, refs, "报价比较缺口未提供 quote_id"))
    if not items:
        items = [ReportItem("缺口", "没有结构化缺口记录；这不表示信息完整", missing_reason="上游未提供结构化缺口，需求也无 unknowns")]
    return ReportSection("gaps", "缺口与待确认项", f"共 {len(items)} 条缺口记录", tuple(items))


def _question_section(questions, facts_by_id, evidence_by_id, quotes_by_id) -> ReportSection:
    items: list[ReportItem] = []
    for question in (questions or {}).get("questions", []) or []:
        candidate_id = str(question["candidate_id"])
        fact_ids = question.get("fact_ids") or _get(question.get("evidence", {}), "fact_ids", [])
        evidence_ids = question.get("evidence_ids") or _get(question.get("evidence", {}), "evidence_ids", [])
        quote_ids = question.get("quote_ids") or _get(question.get("evidence", {}), "quote_ids", [])
        refs = list(_refs_from_ids(fact_ids, evidence_ids, candidate_id, facts_by_id, evidence_by_id))
        for quote_id in sorted({str(value) for value in quote_ids or [] if str(value).strip()}):
            quote_item = quotes_by_id.get(quote_id)
            refs.append(_quote_ref(quote_item) if quote_item else EvidenceReference(
                quote_id=quote_id, candidate_id=candidate_id,
                missing_reason="追问引用的 quote_id 未在报价输入中找到",
            ))
        text = str(question.get("question", ""))
        if question.get("reason"):
            text += f"；原因：{question['reason']}"
        if question.get("priority"):
            text += f"；优先级：{question['priority']}"
        items.append(_item(str(question.get("category", "追问")), text, candidate_id, tuple(refs), "M10 追问允许没有 fact_id/evidence_id，因为缺口可能尚未形成事实"))
    if not items:
        items = [ReportItem("追问", "没有待向供应商确认的结构化问题", missing_reason="M10 问题列表为空")]
    return ReportSection("supplier_questions", "供应商追问清单", f"共 {len(items)} 个追问", tuple(items))

def _report_input_payload(task_id, requirement, candidates, evaluations, facts, quotes, comparisons, ranking, questions) -> dict[str, Any]:
    return {"task_id": task_id, "requirement": requirement, "candidates": candidates, "evaluations": evaluations,
            "facts": facts, "quotes": quotes, "comparisons": comparisons, "ranking": ranking, "questions": questions}


class ReportService:
    """Build deterministic reports and retain immutable version history."""

    def __init__(self, repository: ReportRepository | None = None) -> None:
        self.repository = repository or InMemoryReportRepository()

    def build_report(self, task_id: str, requirement: Any, candidates: Any, evaluations: Any, facts: Any,
                     quotes: Any = (), comparisons: Any = (), questions: Any = None, ranking: Any = None) -> SelectionReport:
        task_id = _required_text(task_id, "task_id")
        requirement_data = _normalise_requirement(requirement)
        candidate_data = _normalise_candidates(candidates)
        evaluation_data = _normalise_evaluations(evaluations)
        fact_data = _normalise_facts(facts)
        quote_data = _normalise_quotes(quotes)
        comparison_data = _normalise_comparisons(comparisons)
        ranking_data = _normalise_ranking(ranking, evaluation_data, fact_data)
        _validate_candidate_references(candidate_data, evaluation_data, fact_data, quote_data, comparison_data, None, ranking_data)
        _validate_ranking_safety(ranking_data, evaluation_data)
        if questions is None:
            recommendation_inputs = ([ranking_data["preferred"]] if ranking_data.get("preferred") else []) + list(ranking_data["alternatives"]) + list(ranking_data["needs_review"]) + list(ranking_data["ineligible"])
            questions_data = _normalise_questions(generate_question_list(evaluation_data, fact_data, quote_data, comparison_data, recommendation_inputs))
        else:
            questions_data = _normalise_questions(questions)
        _validate_candidate_references(candidate_data, evaluation_data, fact_data, quote_data, comparison_data, questions_data, ranking_data)

        candidates_by_id = {candidate["id"]: candidate for candidate in candidate_data}
        facts_by_id = {fact["fact_id"]: fact for fact in fact_data}
        quotes_by_id = {item["quote_id"]: item for item in quote_data}
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for fact in fact_data:
            for evidence in fact.get("evidence", []) or []:
                if evidence.get("evidence_id"):
                    evidence_by_id[str(evidence["evidence_id"])] = {"fact_id": fact["fact_id"], "candidate_id": fact["candidate_id"], **evidence}

        preferred = _recommendation_view(ranking_data["preferred"], facts_by_id, evidence_by_id) if ranking_data.get("preferred") else None
        alternatives = tuple(_recommendation_view(item, facts_by_id, evidence_by_id) for item in ranking_data["alternatives"])
        needs_review = tuple(_recommendation_view(item, facts_by_id, evidence_by_id) for item in ranking_data["needs_review"])
        ineligible = tuple(_recommendation_view(item, facts_by_id, evidence_by_id) for item in ranking_data["ineligible"])
        recommendation_views = ([preferred] if preferred else []) + list(alternatives) + list(needs_review) + list(ineligible)
        sections = (
            _recommendation_section(ranking_data["status"], preferred, alternatives, needs_review, ineligible, candidates_by_id),
            _requirement_section(requirement_data), _candidate_section(candidate_data),
            _hard_constraint_section(evaluation_data, candidates_by_id, facts_by_id, evidence_by_id),
            _fact_section(fact_data, facts_by_id, evidence_by_id), _quote_section(quote_data),
            _risk_section(recommendation_views, facts_by_id, evidence_by_id),
            _gap_section(recommendation_views, requirement_data, comparison_data, facts_by_id, evidence_by_id),
            _question_section(questions_data, facts_by_id, evidence_by_id, quotes_by_id),
        )
        draft = SelectionReport(task_id=task_id, status=ranking_data["status"], preferred=preferred,
                                alternatives=alternatives, needs_review=needs_review, ineligible=ineligible,
                                sections=sections, supplier_questions=tuple(questions_data["questions"]))
        payload = _report_input_payload(task_id, requirement_data, candidate_data, evaluation_data, fact_data, quote_data, comparison_data, ranking_data, questions_data)
        report_payload = draft.to_dict()
        report_payload.pop("report_id", None)
        report_payload.pop("content_hash", None)
        content_hash = _digest({"input": payload, "report": report_payload})
        return replace(draft, report_id="report-" + content_hash[:16], content_hash=content_hash)

    def create_version(self, task_id: str, requirement: Any, candidates: Any, evaluations: Any, facts: Any,
                       quotes: Any = (), comparisons: Any = (), questions: Any = None, ranking: Any = None, *,
                       revision_reason: str | None = None, created_at: datetime | str | None = None,
                       expected_parent_version_id: str | None = None) -> ReportSnapshot:
        report = self.build_report(task_id, requirement, candidates, evaluations, facts, quotes, comparisons, questions, ranking)
        latest = self.repository.latest(report.task_id)
        if latest is not None and latest.report.content_hash == report.content_hash:
            if expected_parent_version_id not in (None, latest.version.version_id):
                raise ModuleValidationError("parent_version_id does not match the current version", code="version_conflict", field="parent_version_id")
            return latest
        if latest is None and expected_parent_version_id is not None:
            raise ModuleValidationError("no parent version exists", code="version_conflict", field="parent_version_id")
        if latest is not None and expected_parent_version_id not in (None, latest.version.version_id):
            raise ModuleValidationError("parent_version_id does not match the current version", code="version_conflict", field="parent_version_id")
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        elif isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError as exc:
                raise ModuleValidationError("created_at must be ISO-8601", code="invalid_created_at", field="created_at") from exc
        if not isinstance(created_at, datetime):
            raise ModuleValidationError("created_at must be datetime", code="invalid_created_at", field="created_at")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if revision_reason is None:
            revision_reason = "initial" if latest is None else "input_changed"
        revision_reason = _required_text(revision_reason, "revision_reason")
        version_number = 1 if latest is None else latest.version.version_number + 1
        parent_version_id = None if latest is None else latest.version.version_id
        version_id = "version-" + sha256(f"{report.task_id}|{version_number}|{report.content_hash}".encode("utf-8")).hexdigest()[:16]
        version = ReportVersion(task_id=report.task_id, version_number=version_number, parent_version_id=parent_version_id,
                                revision_reason=revision_reason, created_at=created_at,
                                content_hash=report.content_hash, version_id=version_id)
        return self.repository.save(ReportSnapshot(version=version, report=report), expected_parent_version_id)

    def export(self, report_or_snapshot: SelectionReport | ReportSnapshot, format: ExportFormat | str = ExportFormat.MARKDOWN) -> ExportResult:
        if isinstance(report_or_snapshot, ReportSnapshot):
            report, version = report_or_snapshot.report, report_or_snapshot.version
        elif isinstance(report_or_snapshot, SelectionReport):
            report, version = report_or_snapshot, None
        else:
            raise ModuleValidationError("export requires SelectionReport or ReportSnapshot", code="invalid_report", field="report")
        if isinstance(format, str) and format.strip().lower() in {"md", "markdown"}:
            format = ExportFormat.MARKDOWN
        elif isinstance(format, str) and format.strip().lower() == "json":
            format = ExportFormat.JSON
        try:
            selected_format = ExportFormat(format)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("format must be markdown or json", code="invalid_export_format", field="format") from exc
        if selected_format is ExportFormat.MARKDOWN:
            content = _markdown(report, version)
        else:
            payload = {"report": report.to_dict(), "version": version.to_dict()} if version else report.to_dict()
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        extension = "md" if selected_format is ExportFormat.MARKDOWN else "json"
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", report.task_id).strip("-") or "task"
        suffix = f"-v{version.version_number}" if version else ""
        return ExportResult(format=selected_format, content=content, filename=f"selection-report-{safe_task_id}{suffix}.{extension}",
                            content_hash=sha256(content.encode("utf-8")).hexdigest(), report_id=report.report_id,
                            version_number=version.version_number if version else None)

    def get_version(self, task_id: str, version_number: int) -> ReportSnapshot | None:
        return self.repository.get(task_id, version_number)

    def history(self, task_id: str) -> list[ReportSnapshot]:
        return self.repository.list_versions(task_id)

    def list_versions(self, task_id: str) -> list[ReportSnapshot]:
        return self.repository.list_versions(task_id)

    def latest(self, task_id: str) -> ReportSnapshot | None:
        return self.repository.latest(task_id)

    generate = build_report
    create = create_version


def _evidence_markdown(reference: Mapping[str, Any]) -> str:
    labels = (("fact_id", "fact_id"), ("evidence_id", "evidence_id"), ("quote_id", "quote_id"),
              ("document_id", "document_id"), ("page", "page"), ("url", "URL"),
              ("body_location", "body"), ("location", "location"), ("source_type", "source_type"))
    parts = [f"{label}={reference[key]}" for key, label in labels if reference.get(key) not in (None, "")]
    if reference.get("price_evidence_level"):
        parts.append(f"price_evidence_level={reference['price_evidence_level']}")
    if reference.get("tender_title"):
        parts.append(f"tender_title={reference['tender_title']}")
    return "; ".join(parts)


def _markdown(report: SelectionReport, version: ReportVersion | None = None) -> str:
    lines = ["# 实验室仪器选型报告", ""]
    if version:
        lines.extend([f"- 版本：v{version.version_number}", f"- 父版本：{version.parent_version_id or '无'}",
                      f"- 修订原因：{version.revision_reason}", f"- 创建时间：{version.created_at.isoformat()}"])
    lines.extend([f"- 任务 ID：{report.task_id}", f"- 报告 ID：{report.report_id}",
                  f"- 结论状态：{report.status.value}", f"- 内容哈希：{report.content_hash}", ""])
    for section in report.sections:
        lines.extend([f"## {section.title}", "", section.summary, ""])
        if not section.items and section.missing_reason:
            lines.extend([f"无内容原因：{section.missing_reason}", ""])
        for item in section.items:
            lines.append(f"- **{item.label}**：{item.text}")
            for reference in item.evidence:
                location = _evidence_markdown(reference.to_dict())
                if reference.missing_reason:
                    location = f"{location}; 证据缺口={reference.missing_reason}" if location else f"证据缺口={reference.missing_reason}"
                lines.append(f"  - 证据：{location}" if location else "  - 证据缺口：未提供")
            if not item.evidence and item.missing_reason:
                lines.append(f"  - 无证据原因：{item.missing_reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(*args: Any, **kwargs: Any) -> SelectionReport:
    return ReportService().build_report(*args, **kwargs)


def create_report_version(*args: Any, repository: ReportRepository | None = None, **kwargs: Any) -> ReportSnapshot:
    return ReportService(repository=repository).create_version(*args, **kwargs)


def export_report(report_or_snapshot: SelectionReport | ReportSnapshot, format: ExportFormat | str = ExportFormat.MARKDOWN) -> ExportResult:
    return ReportService().export(report_or_snapshot, format)


__all__ = ["ReportService", "build_report", "create_report_version", "export_report"]
