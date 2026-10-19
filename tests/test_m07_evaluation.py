from backend.domain.evaluations import ConstraintEvaluationStatus, EvaluationStatus
from backend.domain.facts import FactEvidence, FactEvidenceLevel, FactStatus
from backend.services.evaluation_service import evaluate_candidate, evaluate_candidates
from backend.domain.errors import ModuleValidationError
from backend.services.fact_service import make_fact
from backend.services.recommendation_service import rank_candidates
from backend.services.requirement_service import RequirementService
from datetime import datetime, timezone

def ev(cid="c"):
    return FactEvidence("e-"+cid, cid, "user_document", 90, FactEvidenceLevel.USER_PROVIDED,
                        datetime.now(timezone.utc), document_id="d", page=1)
def fact(cid, name, value, status=FactStatus.CONFIRMED, fid=None, unit=None):
    return make_fact(cid, name, value, "json" if isinstance(value,dict) else ("number" if isinstance(value,(int,float)) else "text"), ev(cid), fact_id=fid, status=status, unit=unit)
def req(hard, soft=None):
    return {"scenario":"x", "hard_constraints":hard, "soft_constraints":soft or [], "status":"confirmed"}

def test_hard_satisfied_and_json_shape():
    out=evaluate_candidate(req([{"name":"price","value":{"max":50000}}]), {"id":"c"}, [fact("c","price",40000)])
    assert out.status is EvaluationStatus.ELIGIBLE and out.eligible
    assert out.to_dict()["hard_constraints"][0]["evidence_ids"] == ["e-c"]
def test_hard_violated_unknown_and_soft():
    bad=evaluate_candidate(req([{"name":"capacity","value":{"min":10}}]), {"id":"c"}, [fact("c","capacity",5)])
    assert bad.status is EvaluationStatus.INELIGIBLE and bad.hard_constraints[0].status is ConstraintEvaluationStatus.VIOLATED
    unk=evaluate_candidate(req([{"name":"temp","value":{"min":10}}], [{"name":"quiet","value":True}]), {"id":"c"}, [])
    assert unk.status is EvaluationStatus.NEEDS_REVIEW and unk.hard_constraints[0].missing_reason == "no_confirmed_fact"
def test_conflict_and_structured_throughput():
    a=fact("c","throughput",{"per_day":20},fid="f1"); b=fact("c","throughput",{"per_day":30},fid="f2")
    a.status=b.status=FactStatus.CONFLICTED
    out=evaluate_candidate(req([{"name":"throughput","value":{"min":10}}]), {"id":"c"}, [a,b])
    assert out.hard_constraints[0].status is ConstraintEvaluationStatus.UNKNOWN
def test_invalid_constraint_rejected():
    try: evaluate_candidate(req([{"bad":1}]), {"id":"c"}, [])
    except Exception as e: assert getattr(e,"code",None)=="invalid_constraint"
    else: raise AssertionError("expected validation error")


def test_empty_requirement_constraints_need_review_and_cannot_be_ranked():
    evaluated = evaluate_candidate(req([]), {"id": "c"}, [])

    assert evaluated.status is EvaluationStatus.NEEDS_REVIEW
    assert not evaluated.eligible
    assert evaluated.hard_constraints[0].status is ConstraintEvaluationStatus.UNKNOWN
    assert evaluated.hard_constraints[0].missing_reason == "no_requirement_constraints"

    ranking = rank_candidates([evaluated], [])
    assert ranking.status.value == "no_recommendation"
    assert ranking.preferred is None
    assert ranking.needs_review[0].gaps[0].missing_reason == "no_requirement_constraints"


def test_canonical_deepseek_fields_match_explicit_throughput_and_budget_values():
    requirement = req([
        {"name": "throughput", "value": {"per_day": 40, "unit": "samples"}},
        {"name": "budget_max", "value": {"amount": 50000, "currency": "CNY"}},
    ])
    facts = [
        fact(
            "c", "capacity_and_throughput",
            {"throughput": {"per_day": 48, "unit": "samples"}}, fid="f-throughput",
        ),
        fact(
            "c", "price_warranty_delivery_and_service",
            {"price": {"amount": 42000, "currency": "CNY"}, "warranty": "2 years"},
            fid="f-price",
        ),
    ]

    evaluated = evaluate_candidate(requirement, {"id": "c"}, facts)

    assert evaluated.status is EvaluationStatus.ELIGIBLE
    assert [item.status for item in evaluated.hard_constraints] == [
        ConstraintEvaluationStatus.SATISFIED,
        ConstraintEvaluationStatus.SATISFIED,
    ]
    assert evaluated.hard_constraints[0].actual_value == {"per_day": 48.0}
    assert evaluated.hard_constraints[1].actual_value == 42000.0


def test_canonical_deepseek_fields_do_not_extract_numbers_from_marketing_prose():
    requirement = req([
        {"name": "throughput", "value": {"per_day": 40, "unit": "samples"}},
        {"name": "price", "value": {"max": 50000, "unit": "CNY"}},
    ])
    facts = [
        fact(
            "c", "capacity_and_throughput",
            {"description": "高通量设计，可兼容 96 孔板", "speed": "200 rpm"},
            fid="f-throughput",
        ),
        fact(
            "c", "price_warranty_delivery_and_service",
            {"description": "高性价比，优惠后约 12 万元起", "warranty": "3 years"},
            fid="f-price",
        ),
    ]

    evaluated = evaluate_candidate(requirement, {"id": "c"}, facts)

    assert evaluated.status is EvaluationStatus.NEEDS_REVIEW
    assert all(
        item.status is ConstraintEvaluationStatus.UNKNOWN
        and item.missing_reason == "uncomparable_value"
        for item in evaluated.hard_constraints
    )


def test_none_fact_slot_is_treated_as_missing_evidence():
    evaluated = evaluate_candidate(
        req([{"name": "capacity", "value": {"min": 10}}]),
        {"id": "c"},
        [None],
    )

    assert evaluated.status is EvaluationStatus.NEEDS_REVIEW
    assert evaluated.hard_constraints[0].missing_reason == "no_confirmed_fact"


def test_batch_inputs_report_domain_errors_instead_of_type_errors():
    try:
        evaluate_candidates(req([]), None, [])
    except ModuleValidationError as exc:
        assert exc.code == "invalid_candidates"
        assert exc.field == "candidates"
    else:
        raise AssertionError("expected candidates validation error")

    try:
        evaluate_candidates(req([]), [{"id": "c"}], None)
    except ModuleValidationError as exc:
        assert exc.code == "invalid_facts"
        assert exc.field == "facts"
    else:
        raise AssertionError("expected facts validation error")


def test_missing_candidate_object_reports_domain_error():
    try:
        evaluate_candidate(req([]), None, [])
    except ModuleValidationError as exc:
        assert exc.code == "invalid_candidate"
    else:
        raise AssertionError("expected candidate validation error")
