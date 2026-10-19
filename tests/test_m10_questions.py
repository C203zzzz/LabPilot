import json

import pytest

from backend.domain.errors import ModuleValidationError
from backend.domain.questions import QuestionPriority
from backend.services.question_service import generate_question_list


def constraint(name, status="unknown", kind="hard", facts=(), evidence=()):
    return {"constraint_name": name, "kind": kind, "status": status,
            "reason": "结构化判断缺少可靠依据", "fact_ids": list(facts),
            "evidence_ids": list(evidence)}


def evaluation(candidate_id, status="needs_review", hard=(), soft=()):
    return {"candidate_id": candidate_id, "status": status,
            "hard_constraints": list(hard), "soft_constraints": list(soft)}


def complete_quote(candidate_id="c1", quote_id="q1"):
    return {"quote_id": quote_id, "candidate_id": candidate_id,
            "source_type": "current_supplier_quote", "amount": 100000,
            "currency": "CNY", "configuration": {"model": "M1", "kit": "standard"},
            "valid_until": "2026-10-31", "conditions": [
                {"condition_type": "tax", "value": "含税 13%"},
                {"condition_type": "delivery", "value": "30 天"},
                {"condition_type": "installation", "value": "包含"},
                {"condition_type": "warranty", "value": "2 年"},
                {"condition_type": "after_sales", "value": "24 小时响应"},
                {"condition_type": "payment", "value": "验收后付款"},
            ]}


def test_hard_unknown_and_conflicted_fact_are_gap_driven_and_linked():
    hard = constraint("throughput", facts=("f1",), evidence=("e1",))
    conflict = {"candidate_id": "c1", "fact_id": "f1", "field_name": "throughput",
                "status": "conflicted", "evidence": [{"evidence_id": "e1"}, {"evidence_id": "e2"}]}
    result = generate_question_list([evaluation("c1", hard=(hard,))], [conflict])
    assert len(result.questions) == 1
    question = result.questions[0]
    assert question.category.value == "hard_constraint"
    assert question.priority is QuestionPriority.HIGH
    assert question.evidence.fact_ids == ("f1",)
    assert question.evidence.evidence_ids == ("e1", "e2")
    assert question.candidate_disposition.value == "needs_review"
    assert "入围复核" in question.reason


def test_conflicted_fact_without_evaluation_still_asks_for_clarification():
    fact = {"candidate_id": "c1", "fact_id": "f2", "field_name": "detection_limit",
            "status": "conflicted", "evidence": [{"evidence_id": "e2"}]}
    question = generate_question_list(facts=[fact]).questions[0]
    assert question.field == "detection_limit"
    assert "冲突" in question.reason
    assert question.evidence.fact_ids == ("f2",)


def test_missing_quote_fields_and_conditions_only_ask_for_actual_gaps():
    quote = complete_quote()
    quote.update(amount="unknown", currency="unknown", configuration={}, valid_until=None)
    for condition in quote["conditions"]:
        if condition["condition_type"] in {"tax", "delivery", "installation", "warranty", "after_sales", "payment"}:
            condition["value"] = "unknown"
    result = generate_question_list(quotes=[quote])
    fields = {q.field for q in result.questions}
    assert {"quote_amount", "quote_currency", "quote_configuration", "quote_valid_until",
            "quote_tax", "quote_delivery", "quote_installation", "quote_warranty",
            "quote_after_sales", "quote_payment"} == fields
    assert all(q.evidence.quote_ids == ("q1",) for q in result.questions)
    assert generate_question_list(quotes=[complete_quote()]).questions == ()


def test_historical_tender_requests_current_quote():
    quote = complete_quote()
    quote["source_type"] = "historical_tender_price"
    result = generate_question_list(quotes=[quote])
    question = next(q for q in result if q.field == "current_quote")
    assert "历史招标价" in question.reason and "当前有效" in question.question


def test_configuration_or_currency_mismatch_requests_comparable_quote():
    comparison = {"candidate_id": "c1", "incomparable": [
        {"quote_ids": ["q2", "q1"], "reason": "configuration_mismatch;currency_mismatch"}]}
    question = generate_question_list(comparisons=[comparison]).questions[0]
    assert question.category.value == "price_comparability"
    assert question.evidence.quote_ids == ("q1", "q2")
    assert "完全相同" in question.question


def test_known_fields_are_not_reasked_and_ineligible_only_gets_review_question():
    known = constraint("capacity", status="satisfied", facts=("f1",), evidence=("e1",))
    violated = constraint("biosafety", status="violated", facts=("f2",), evidence=("e2",))
    result = generate_question_list(
        [evaluation("ok", status="eligible", hard=(known,)),
         evaluation("out", status="ineligible", hard=(violated,))],
        quotes=[complete_quote("out", "q-out")],
        recommendations=[{"candidate_id": "ok", "category": "preferred"},
                         {"candidate_id": "out", "category": "ineligible"}],
    )
    assert [q.candidate_id for q in result.questions] == ["out"]
    assert result.questions[0].category.value == "review"
    assert result.questions[0].candidate_disposition.value == "ineligible"
    assert "仅复核" in result.questions[0].question


def test_recommendation_mapping_from_api_payload_is_supported():
    result = generate_question_list(
        [evaluation("ok", hard=(constraint("capacity"),))],
        recommendations={"status": "no_recommendation", "preferred": None, "needs_review": [{"candidate_id": "ok", "category": "needs_review"}]},
    )
    assert result.questions and result.questions[0].candidate_id == "ok"


def test_candidate_groups_priority_deduplication_and_sort_are_stable():
    evaluations = [
        evaluation("b", hard=(constraint("z"),), soft=(constraint("a", kind="soft"),)),
        evaluation("a", hard=(constraint("x"), constraint("x"))),
    ]
    first = generate_question_list(evaluations)
    second = generate_question_list(list(reversed(evaluations)))
    assert first.to_dict() == second.to_dict()
    assert list(first.by_candidate) == ["a", "b"]
    assert [q.candidate_id for q in first.questions] == ["a", "b", "b"]
    assert [q.priority.value for q in first.by_candidate["b"]] == ["high", "medium"]
    assert len([q for q in first.questions if q.candidate_id == "a" and q.field == "x"]) == 1


def test_deduplication_merges_quote_associations():
    first = complete_quote("c1", "q1")
    second = complete_quote("c1", "q2")
    first["valid_until"] = second["valid_until"] = None
    questions = generate_question_list(quotes=[second, first]).questions
    validity = next(question for question in questions if question.field == "quote_valid_until")
    assert validity.evidence.quote_ids == ("q1", "q2")


def test_empty_json_serialization_and_explicit_validation_errors():
    empty = generate_question_list()
    assert empty.questions == () and empty.to_dict()["candidate_groups"] == {}
    json.dumps(empty.to_dict(), ensure_ascii=False)
    with pytest.raises(ModuleValidationError) as invalid:
        generate_question_list(evaluations={})
    assert invalid.value.code == "invalid_input" and invalid.value.field == "evaluations"
    with pytest.raises(ModuleValidationError) as missing:
        generate_question_list(quotes=[{"candidate_id": "c1"}])
    assert missing.value.code == "missing_association_id" and missing.value.field == "quotes"
    with pytest.raises(ModuleValidationError) as missing_pair:
        generate_question_list(comparisons=[{"candidate_id": "c1", "incomparable": [{"reason": "currency_mismatch"}]}])
    assert missing_pair.value.code == "missing_association_id"
    with pytest.raises(ModuleValidationError) as missing_evidence:
        generate_question_list(facts=[{"candidate_id": "c1", "fact_id": "f1",
                                      "field_name": "capacity", "status": "conflicted",
                                      "evidence": [{}]}])
    assert missing_evidence.value.code == "missing_association_id"
