"""Deterministic, structured-gap-driven M10 supplier questions."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace

from backend.domain.errors import ModuleValidationError
from backend.domain.evaluations import ConstraintEvaluationStatus, EvaluationStatus
from backend.domain.facts import FactStatus
from backend.domain.questions import (
    CandidateDisposition, QuestionCategory, QuestionEvidence, QuestionList,
    QuestionPriority, SupplierQuestion,
)
from backend.domain.quotes import QuoteSourceType


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _value(value):
    return getattr(value, "value", value)


def _missing(value):
    return (value is None or
            isinstance(value, (Mapping, list, tuple)) and not value or
            isinstance(value, str) and value.strip().lower() in {"", "unknown", "未知", "未说明"})


def _question_id(candidate_id, category, field):
    raw = f"{candidate_id}|{category}|{field}".encode("utf-8")
    return "m10-" + hashlib.sha1(raw).hexdigest()[:12]


def _make(candidate_id, category, priority, field, question, reason, answer_format,
          disposition=CandidateDisposition.UNCLASSIFIED, fact_ids=(), evidence_ids=(), quote_ids=()):
    refs = QuestionEvidence(candidate_id, field, tuple(sorted(set(fact_ids))),
                            tuple(sorted(set(evidence_ids))), tuple(sorted(set(quote_ids))))
    return SupplierQuestion(_question_id(candidate_id, category.value, field), candidate_id,
                            question, reason, category, priority, answer_format, refs, disposition)


def _recommendation_dispositions(recommendations):
    if recommendations in (None, ()):
        return {}
    result = {}
    if hasattr(recommendations, "preferred") or isinstance(recommendations, Mapping):
        preferred = _get(recommendations, "preferred")
        if preferred:
            result[str(_get(preferred, "candidate_id"))] = CandidateDisposition.PREFERRED
        for attr, disposition in (("alternatives", CandidateDisposition.ALTERNATIVE),
                                  ("needs_review", CandidateDisposition.NEEDS_REVIEW),
                                  ("ineligible", CandidateDisposition.INELIGIBLE)):
            for item in (_get(recommendations, attr, ()) or ()):
                result[str(_get(item, "candidate_id"))] = disposition
        return result
    if not isinstance(recommendations, (list, tuple)):
        raise ModuleValidationError("recommendations must be a RankingResult or list", code="invalid_input", field="recommendations")
    for item in recommendations:
        candidate_id, category = _get(item, "candidate_id"), _value(_get(item, "category"))
        if not candidate_id:
            raise ModuleValidationError("recommendation missing candidate_id", code="missing_association_id", field="recommendations")
        try:
            result[str(candidate_id)] = CandidateDisposition(category)
        except (TypeError, ValueError) as exc:
            raise ModuleValidationError("invalid recommendation category", code="invalid_input", field="recommendations") from exc
    return result


def generate_question_list(evaluations=(), facts=(), quotes=(), comparisons=(), recommendations=()):
    """Build a stable list; no templates are emitted without a structured gap."""
    for name, items in (("evaluations", evaluations), ("facts", facts),
                        ("quotes", quotes), ("comparisons", comparisons)):
        if not isinstance(items, (list, tuple)):
            raise ModuleValidationError(f"{name} must be a list", code="invalid_input", field=name)

    dispositions = _recommendation_dispositions(recommendations)
    evaluation_status = {}
    questions, positions = [], {}

    def disposition_for(candidate_id):
        return dispositions.get(candidate_id, {
            EvaluationStatus.ELIGIBLE.value: CandidateDisposition.ALTERNATIVE,
            EvaluationStatus.NEEDS_REVIEW.value: CandidateDisposition.NEEDS_REVIEW,
            EvaluationStatus.INELIGIBLE.value: CandidateDisposition.INELIGIBLE,
        }.get(evaluation_status.get(candidate_id), CandidateDisposition.UNCLASSIFIED))

    def add(question):
        key = (question.candidate_id, question.field)
        if key not in positions:
            positions[key] = len(questions)
            questions.append(question)
            return
        position = positions[key]
        current = questions[position]
        merged = QuestionEvidence(
            current.candidate_id, current.field,
            tuple(sorted(set(current.evidence.fact_ids + question.evidence.fact_ids))),
            tuple(sorted(set(current.evidence.evidence_ids + question.evidence.evidence_ids))),
            tuple(sorted(set(current.evidence.quote_ids + question.evidence.quote_ids))),
        )
        questions[position] = replace(current, evidence=merged)

    for evaluation in evaluations:
        candidate_id = str(_get(evaluation, "candidate_id", "")).strip()
        if not candidate_id:
            raise ModuleValidationError("evaluation missing candidate_id", code="missing_association_id", field="evaluations")
        status = str(_value(_get(evaluation, "status", "")))
        if status not in {x.value for x in EvaluationStatus}:
            raise ModuleValidationError("invalid evaluation status", code="invalid_input", field="evaluations")
        evaluation_status[candidate_id] = status

    for evaluation in evaluations:
        candidate_id = str(_get(evaluation, "candidate_id"))
        disposition = disposition_for(candidate_id)
        constraints = list(_get(evaluation, "hard_constraints", ()) or ()) + list(_get(evaluation, "soft_constraints", ()) or ())
        for constraint in constraints:
            field = str(_get(constraint, "constraint_name", "")).strip()
            if not field:
                raise ModuleValidationError("constraint missing name", code="invalid_input", field="evaluations")
            status = str(_value(_get(constraint, "status", "")))
            kind = str(_get(constraint, "kind", ""))
            fact_ids = _get(constraint, "fact_ids", ()) or ()
            evidence_ids = _get(constraint, "evidence_ids", ()) or ()
            if status == ConstraintEvaluationStatus.UNKNOWN.value and disposition is not CandidateDisposition.INELIGIBLE:
                category = QuestionCategory.HARD_CONSTRAINT if kind == "hard" else QuestionCategory.FACT
                prefix = "为完成入围复核，" if disposition is CandidateDisposition.NEEDS_REVIEW else "为完善首选/备选比较，"
                add(_make(candidate_id, category,
                    QuestionPriority.HIGH if category is QuestionCategory.HARD_CONSTRAINT else QuestionPriority.MEDIUM,
                    field, f"请确认贵司设备 {field} 的实际值、测试条件及适用配置。",
                    prefix + str(_get(constraint, "reason", "现有资料无法确认该字段。")),
                    "明确数值或枚举值，并附测试条件、配置名称及官方规格页。", disposition,
                    fact_ids, evidence_ids))
            if status == ConstraintEvaluationStatus.VIOLATED.value and disposition is CandidateDisposition.INELIGIBLE and kind == "hard":
                add(_make(candidate_id, QuestionCategory.REVIEW, QuestionPriority.HIGH, field,
                    f"请仅复核贵司设备的 {field} 是否确实不满足本次要求；如有满足要求的可选配置，请列明完整型号。",
                    "候选已因硬约束不满足而淘汰，只核对可能的配置或口径误判。",
                    "回答满足或不满足；如需纠正，请提供完整型号、配置及规格页。", disposition,
                    fact_ids, evidence_ids))

    for fact in facts:
        candidate_id = str(_get(fact, "candidate_id", "")).strip()
        fact_id = str(_get(fact, "fact_id", "")).strip()
        field = str(_get(fact, "field_name", "")).strip()
        if not candidate_id or not fact_id or not field:
            raise ModuleValidationError("fact missing association id", code="missing_association_id", field="facts")
        disposition = disposition_for(candidate_id)
        if disposition is CandidateDisposition.INELIGIBLE:
            continue
        status = str(_value(_get(fact, "status", "")))
        if status not in {FactStatus.CONFLICTED.value, FactStatus.UNKNOWN.value}:
            continue
        evidence = tuple(_get(fact, "evidence", ()) or ())
        if any(not _get(item, "evidence_id") for item in evidence):
            raise ModuleValidationError("fact evidence missing evidence_id", code="missing_association_id", field="facts")
        evidence_ids = tuple(str(_get(e, "evidence_id")) for e in evidence)
        reason = "现有证据对该事实存在冲突，需要供应商书面澄清。" if status == FactStatus.CONFLICTED.value else "该关键事实尚未确认。"
        add(_make(candidate_id, QuestionCategory.FACT, QuestionPriority.HIGH, field,
                  f"请书面确认贵司设备 {field} 的准确值，并说明适用型号、配置及测试条件。",
                  reason, "给出唯一明确值，并附对应规格页或检测依据。", disposition,
                  (fact_id,), evidence_ids))

    for quote in quotes:
        candidate_id = str(_get(quote, "candidate_id", "")).strip()
        quote_id = str(_get(quote, "quote_id", "")).strip()
        if not candidate_id or not quote_id:
            raise ModuleValidationError("quote missing association id", code="missing_association_id", field="quotes")
        disposition = disposition_for(candidate_id)
        if disposition is CandidateDisposition.INELIGIBLE:
            continue
        source_type = str(_value(_get(quote, "source_type", "")))
        if source_type == QuoteSourceType.HISTORICAL_TENDER_PRICE.value:
            add(_make(candidate_id, QuestionCategory.QUOTE, QuestionPriority.HIGH, "current_quote",
                "请提供当前有效的正式报价，并列明报价日期、有效期、币种、税务口径和完整配置。",
                "历史招标价只能作为历史参考，不能代表当前供应商报价。",
                "正式报价单 PDF 或盖章文件，逐项列明金额与条件。", disposition, quote_ids=(quote_id,)))
        for field, text, fmt in (
            ("amount", "请明确本配置的报价金额。", "数值金额，不使用面议或区间代替。"),
            ("currency", "请明确报价币种。", "三位币种代码，例如 CNY、USD。"),
            ("configuration", "请列明本报价对应的完整型号、配置、附件和软件。", "配置清单，逐项注明数量和版本。"),
            ("valid_until", "请明确本报价的有效期。", "截止日期 YYYY-MM-DD，或明确有效天数。"),
        ):
            if _missing(_get(quote, field)):
                add(_make(candidate_id, QuestionCategory.QUOTE, QuestionPriority.HIGH, "quote_" + field,
                    text, f"报价的 {field} 字段缺失，当前无法形成可执行采购条件。", fmt,
                    disposition, quote_ids=(quote_id,)))
        conditions = {str(_value(_get(c, "condition_type"))): _get(c, "value") for c in (_get(quote, "conditions", ()) or ())}
        labels = {"tax":"税务（含税/未税及税率）", "delivery":"交付周期", "installation":"安装调试",
                  "warranty":"质保", "after_sales":"售后响应", "payment":"付款"}
        for condition, label in labels.items():
            if condition not in conditions or _missing(conditions[condition]):
                category = QuestionCategory.SERVICE if condition != "tax" else QuestionCategory.QUOTE
                add(_make(candidate_id, category, QuestionPriority.HIGH, "quote_" + condition,
                    f"请明确本报价的{label}条件。", f"{label}条件未知，无法评估采购风险和总成本。",
                    "给出明确条款、期限、范围及不包含事项。", disposition, quote_ids=(quote_id,)))

    for comparison in comparisons:
        candidate_id = str(_get(comparison, "candidate_id", "")).strip()
        if not candidate_id:
            raise ModuleValidationError("comparison missing candidate_id", code="missing_association_id", field="comparisons")
        disposition = disposition_for(candidate_id)
        if disposition is CandidateDisposition.INELIGIBLE:
            continue
        for pair in (_get(comparison, "incomparable", ()) or ()):
            quote_ids = tuple(_get(pair, "quote_ids", ()) or ())
            if not quote_ids or any(not str(x).strip() for x in quote_ids):
                raise ModuleValidationError("comparison missing quote_ids", code="missing_association_id", field="comparisons")
            reasons = str(_get(pair, "reason", ""))
            if any(reason in reasons for reason in ("configuration_mismatch", "currency_mismatch", "tax_condition_mismatch")):
                add(_make(candidate_id, QuestionCategory.PRICE_COMPARABILITY, QuestionPriority.HIGH,
                    "quote_comparability", "请按完全相同的配置、币种及含税/未税口径重新提供报价，以便公平比较。",
                    "现有报价配置、币种或税务口径不同，金额不可直接比较。",
                    "逐项列出完整配置、币种、税率及含税总价。", disposition, quote_ids=quote_ids))

    priority_order = {QuestionPriority.HIGH: 0, QuestionPriority.MEDIUM: 1, QuestionPriority.LOW: 2}
    questions.sort(key=lambda q: (q.candidate_id, priority_order[q.priority], q.category.value,
                                  q.field or "", q.question_id))
    return QuestionList(tuple(questions))


class QuestionService:
    generate = staticmethod(generate_question_list)


__all__ = ["generate_question_list", "QuestionService"]
