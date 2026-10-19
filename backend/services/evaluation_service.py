"""Pure, conservative M07 constraint evaluation service."""
from __future__ import annotations

import re
from typing import Any

from backend.domain.errors import ModuleValidationError
from backend.domain.evaluations import *
from backend.domain.facts import FactStatus
from backend.domain.requirements import RequirementSpec
from backend.domain.requirements_improved import RequirementSpec as ImmutableRequirementSpec

_MISSING = object()

_FIELD_ALIASES = {
    "budget_max": {
        "budget_max", "budget", "price", "price_amount",
        "price_warranty_delivery_and_service",
    },
    "budget": {
        "budget", "budget_max", "price", "price_amount",
        "price_warranty_delivery_and_service",
    },
    "price": {
        "price", "price_amount", "budget",
        "price_warranty_delivery_and_service",
    },
    "price_max": {
        "price_max", "price", "price_amount", "budget",
        "price_warranty_delivery_and_service",
    },
    "throughput": {
        "throughput", "capacity", "throughput_per_day",
        "capacity_and_throughput",
    },
}

_THROUGHPUT_KEYS = {
    "per_day": ("per_day", "throughput_per_day", "samples_per_day"),
    "per_hour": ("per_hour", "throughput_per_hour", "samples_per_hour"),
    "per_run": ("per_run", "throughput_per_run", "samples_per_run", "capacity_per_run"),
}

_PRICE_KEYS = ("price_amount", "price", "amount", "current_price", "total_price")

def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _immutable_requirement(requirement):
    """Take an immutable validation snapshot without changing the wire contract.

    The persisted/API requirement still uses the legacy mutable model.  Valid
    inputs are converted to the hardened frozen model for evaluation so its
    tuple-backed constraints and constructor checks are active in production.
    A legacy record containing a field the hardened model cannot represent is
    evaluated through the old parser, preserving backwards compatibility for
    already-persisted data.
    """
    raw = requirement.to_dict() if isinstance(requirement, RequirementSpec) else requirement
    if not isinstance(raw, dict):
        raise ModuleValidationError(
            "需求对象必须是 JSON 对象",
            code="invalid_requirement",
            field="requirement",
        )
    # Do not fall back to the permissive mutable parser.  Invalid budget,
    # version, status or constraint values must be rejected consistently on
    # the API path just as they are in the hardened domain tests.
    return ImmutableRequirementSpec.from_dict(raw)

def _facts_for(facts, candidate_id, name):
    normalized_name = str(name).strip().casefold()
    names = _FIELD_ALIASES.get(normalized_name, {normalized_name})
    return [f for f in facts if f is not None and str(_get(f,"candidate_id","")) == str(candidate_id)
            and str(_get(f,"field_name","")).strip().casefold() in names]

def _refs(fs):
    ids=[]; evid=[]
    for f in fs:
        if f is None:
            continue
        if _get(f,"fact_id"): ids.append(str(_get(f,"fact_id")))
        for e in (_get(f,"evidence",[]) or []):
            if _get(e,"evidence_id"): evid.append(str(_get(e,"evidence_id")))
    return sorted(set(ids)), sorted(set(evid))

def _status_value(value):
    return getattr(value, "value", value)

def _numeric(v):
    if isinstance(v, bool): return None
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,str):
        try: return float(v.strip().replace(",", ""))
        except ValueError: return None
    return None

def _unit(value):
    if value is None:
        return None
    normalized = str(value).strip().casefold().replace(" ", "")
    aliases = {
        "rmb": "cny", "人民币": "cny", "元": "cny", "¥": "cny", "￥": "cny",
        "sample": "samples", "样品": "samples", "个样品": "samples", "份样品": "samples",
    }
    return aliases.get(normalized, normalized)

def _strict_price(value, explicit_unit=None):
    """Return a price only when the whole scalar is an unambiguous amount."""
    if isinstance(value, bool):
        return _MISSING, None
    if isinstance(value, dict):
        nested_unit = value.get("currency") or value.get("unit") or explicit_unit
        candidates = []
        for key in ("amount", "price_amount", "value"):
            if key not in value:
                continue
            amount, parsed_unit = _strict_price(value[key], nested_unit)
            if amount is not _MISSING:
                candidates.append((amount, parsed_unit))
        distinct = set(candidates)
        if len(distinct) != 1:
            return _MISSING, None
        return distinct.pop()
    if isinstance(value, (int, float)):
        return float(value), _unit(explicit_unit)
    if not isinstance(value, str):
        return _MISSING, None
    match = re.fullmatch(
        r"\s*(?:(?P<prefix>CNY|RMB|人民币|[¥￥])\s*)?"
        r"(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)"
        r"\s*(?P<suffix>CNY|RMB|人民币|元)?\s*",
        value,
        flags=re.I,
    )
    if not match:
        return _MISSING, None
    parsed_unit = _unit(match.group("prefix") or match.group("suffix") or explicit_unit)
    if parsed_unit is None:
        return _MISSING, None
    return float(match.group("amount").replace(",", "")), parsed_unit

def _strict_throughput(value, explicit_unit=None):
    """Parse only standalone throughput measurements, never prose with numbers."""
    if isinstance(value, dict):
        containers = [value]
        if isinstance(value.get("throughput"), dict):
            containers.append(value["throughput"])
        parsed = {}
        for period, aliases in _THROUGHPUT_KEYS.items():
            matches = [item[key] for item in containers for key in aliases if key in item]
            numeric = {_numeric(item) for item in matches}
            numeric.discard(None)
            for item in matches:
                measured, _ = _strict_throughput(item, explicit_unit)
                if measured is not _MISSING and period in measured:
                    numeric.add(measured[period])
            if len(numeric) == 1:
                parsed[period] = numeric.pop()
            elif len(numeric) > 1:
                return _MISSING, None
        if not parsed:
            for key in ("throughput", "capacity", "processing_capacity"):
                if key not in value or isinstance(value[key], dict):
                    continue
                measured, measured_unit = _strict_throughput(
                    value[key], value.get("unit") or explicit_unit,
                )
                if measured is not _MISSING:
                    return measured, measured_unit
        if not parsed:
            return _MISSING, None
        raw_unit = value.get("unit") or next(
            (item.get("unit") for item in containers if item.get("unit")),
            explicit_unit,
        )
        normalized_unit = _unit(raw_unit)
        if normalized_unit in {
            "sample/day", "sample/d", "sample/hour", "sample/h", "sample/run",
            "samples/day", "samples/d", "samples/hour", "samples/h", "samples/run",
        }:
            normalized_unit = "samples"
        return parsed, normalized_unit
    if isinstance(value, bool):
        return _MISSING, None
    if isinstance(value, (int, float)):
        normalized_unit = _unit(explicit_unit)
        period_units = {
            "sample/day": "per_day", "sample/d": "per_day",
            "samples/day": "per_day", "samples/d": "per_day",
            "sample/hour": "per_hour", "sample/h": "per_hour",
            "samples/hour": "per_hour", "samples/h": "per_hour",
            "sample/run": "per_run", "samples/run": "per_run",
        }
        period = period_units.get(normalized_unit)
        if period is None:
            return _MISSING, None
        return {period: float(value)}, "samples"
    if not isinstance(value, str):
        return _MISSING, None
    match = re.fullmatch(
        r"\s*(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)\s*"
        r"(?P<sample>samples?|样品|个样品|份样品)\s*"
        r"(?:/|每|per\s*)(?P<period>day|d|天|日|hour|h|小时|run|次|批)\s*",
        value,
        flags=re.I,
    )
    if not match:
        return _MISSING, None
    period = match.group("period").casefold()
    key = (
        "per_day" if period in {"day", "d", "天", "日"}
        else "per_hour" if period in {"hour", "h", "小时"}
        else "per_run"
    )
    return {key: float(match.group("amount").replace(",", ""))}, "samples"

def _constraint_actual(fact, constraint_name):
    """Project grouped DeepSeek fields onto a constraint without guessing."""
    field_name = str(_get(fact, "field_name", "")).strip().casefold()
    value = _get(fact, "value")
    fact_unit = _get(fact, "unit")
    if field_name == "capacity_and_throughput":
        return _strict_throughput(value, fact_unit)
    if field_name == "price_warranty_delivery_and_service":
        if isinstance(value, dict):
            candidates = []
            embedded_unit = value.get("currency") or value.get("unit") or fact_unit
            for key in _PRICE_KEYS:
                if key not in value:
                    continue
                amount, parsed_unit = _strict_price(value[key], embedded_unit)
                if amount is not _MISSING:
                    candidates.append((amount, parsed_unit))
            distinct = {(amount, parsed_unit) for amount, parsed_unit in candidates}
            if len(distinct) != 1:
                return _MISSING, None
            return distinct.pop()
        return _strict_price(value, fact_unit)
    return value, _unit(fact_unit)

def _expected_unit(expected):
    if not isinstance(expected, dict):
        return None
    return _unit(expected.get("unit") or expected.get("currency"))

def _compare(actual, expected, constraint):
    name = str(_get(constraint, "name", "")).strip().casefold()
    op = "le" if name in {"budget_max", "price_max"} else "eq"
    exp = expected; unit = None
    if isinstance(expected, dict):
        unit = expected.get("unit")
        op = str(expected.get("operator", expected.get("op", op))).lower()
        if "amount" in expected: exp = expected["amount"]
        elif "max_amount" in expected: exp, op = expected["max_amount"], "le"
        elif "min_amount" in expected: exp, op = expected["min_amount"], "ge"
        elif "value" in expected: exp = expected["value"]
        elif "max" in expected: exp, op = expected["max"], "le"
        elif "min" in expected: exp, op = expected["min"], "ge"
        has_explicit_operator = "operator" in expected or "op" in expected
        if not has_explicit_operator and name in {"budget_max", "price_max"} and "amount" in expected:
            op = "le"
        elif not has_explicit_operator and name == "throughput" and any(key in expected for key in _THROUGHPUT_KEYS):
            op = "ge"
    actual_unit = _get(constraint, "unit")
    # Units are deliberately not converted; an explicit mismatch is unknown.
    if unit and actual_unit and str(unit).lower() != str(actual_unit).lower(): return None
    # Structured throughput/capacity values compare the explicitly requested key.
    if isinstance(actual, dict) and isinstance(exp, dict):
        key = next((k for k in ("per_day", "per_hour", "value", "amount") if k in exp), None)
        if key is None: return None
        actual, exp = actual.get(key, _MISSING), exp[key]
    elif isinstance(actual, dict) and isinstance(exp, (int,float)):
        key = next((k for k in ("per_day", "per_hour", "value", "amount") if k in actual), None)
        if key is None: return None
        actual = actual[key]
    a,n = _numeric(actual), _numeric(exp)
    if a is not None and n is not None:
        return {"eq":a==n,"=":a==n,"lt":a<n,"le":a<=n,"lte":a<=n,"gt":a>n,"ge":a>=n,"gte":a>=n}.get(op)
    if op not in ("eq", "="): return None
    if isinstance(actual, str) and isinstance(exp, str): return actual.strip().casefold() == exp.strip().casefold()
    return actual == exp

def _evaluate_one(c, kind, facts, candidate_id):
    name = str(_get(c,"name","")).strip(); expected = _get(c,"value",_MISSING)
    if not name or expected is _MISSING: raise ModuleValidationError("约束项格式无效", code="invalid_constraint", field=f"{kind}_constraints")
    if str(_get(c, "status", "")).lower() == ConstraintEvaluationStatus.NOT_APPLICABLE.value:
        return ConstraintEvaluation(name, kind, ConstraintEvaluationStatus.NOT_APPLICABLE,
                                    "约束被标记为不适用", missing_reason="not_applicable", expected_value=expected)
    fs = _facts_for(facts,candidate_id,name); ids,eids = _refs(fs)
    usable=[f for f in fs if _status_value(_get(f,"status","")) == FactStatus.CONFIRMED.value]
    if any(_status_value(_get(f,"status","")) == FactStatus.CONFLICTED.value for f in fs):
        return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"事实存在冲突，不能确定满足",ids,eids,"conflicted_fact",None,expected)
    if not usable:
        return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"缺少已确认事实",ids,eids,"no_confirmed_fact",None,expected)
    usable.sort(key=lambda fact: str(_get(fact, "field_name", "")).strip().casefold() != name)
    f=usable[0]
    actual, fact_unit = _constraint_actual(f, name)
    expected_unit = _expected_unit(expected) or _unit(_get(c, "unit"))
    if actual is _MISSING:
        return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"聚合字段没有可安全比较的结构化值",ids,eids,"uncomparable_value",_get(f,"value"),expected)
    if expected_unit and not fact_unit:
        return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"事实缺少约束要求的单位或币种",ids,eids,"unit_missing",actual,expected)
    if expected_unit and fact_unit and expected_unit != fact_unit:
        return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"事实与约束单位不可比",ids,eids,"unit_mismatch",actual,expected)
    result=_compare(actual,expected,c)
    if result is None: return ConstraintEvaluation(name,kind,ConstraintEvaluationStatus.UNKNOWN,"数值或单位无法安全比较",ids,eids,"uncomparable_value",actual,expected)
    st=ConstraintEvaluationStatus.SATISFIED if result else ConstraintEvaluationStatus.VIOLATED
    return ConstraintEvaluation(name,kind,st,f"事实值 {actual!r} {'满足' if result else '不满足'}约束",ids,eids,None,actual,expected)

def evaluate_candidate(requirement, candidate, facts) -> CandidateEvaluation:
    requirement = _immutable_requirement(requirement)
    cid=str(_get(candidate,"id",_get(candidate,"candidate_id","")))
    if not cid: raise ModuleValidationError("候选缺少 id", code="invalid_candidate", field="candidate")
    if not isinstance(facts,(list,tuple)): raise ModuleValidationError("facts 必须是列表", code="invalid_facts", field="facts")
    # Missing extracted fact slots are equivalent to absent evidence, not a
    # reason to crash the entire candidate evaluation batch.
    facts = [fact for fact in facts if fact is not None]
    hard=[_evaluate_one(c,"hard",facts,cid) for c in (requirement.hard_constraints or [])]
    soft=[_evaluate_one(c,"soft",facts,cid) for c in (requirement.soft_constraints or [])]
    if not hard and not soft:
        hard.append(ConstraintEvaluation(
            "requirement_constraints", "hard", ConstraintEvaluationStatus.UNKNOWN,
            "需求没有硬约束或软约束，不能据此判定候选合格",
            missing_reason="no_requirement_constraints",
        ))
        status=EvaluationStatus.NEEDS_REVIEW; reason="需求约束为空，需先补充并确认选型条件"
    elif any(x.status is ConstraintEvaluationStatus.VIOLATED for x in hard): status=EvaluationStatus.INELIGIBLE; reason="存在违反的硬约束"
    elif any(x.status is ConstraintEvaluationStatus.UNKNOWN for x in hard): status=EvaluationStatus.NEEDS_REVIEW; reason="硬约束存在未知项"
    else: status=EvaluationStatus.ELIGIBLE; reason="全部硬约束已满足"
    return CandidateEvaluation(cid,status,status is EvaluationStatus.ELIGIBLE,hard,soft,reason)

class EvaluationService:
    evaluate = staticmethod(evaluate_candidate)

def evaluate_candidates(requirement, candidates, facts):
    """Evaluate a batch without sorting or changing candidate order."""
    if not isinstance(candidates, (list, tuple)):
        raise ModuleValidationError("candidates 必须是列表", code="invalid_candidates", field="candidates")
    if not isinstance(facts, (list, tuple)):
        raise ModuleValidationError("facts 必须是列表", code="invalid_facts", field="facts")
    return [evaluate_candidate(requirement, candidate, facts) for candidate in candidates]

__all__=["evaluate_candidate","evaluate_candidates","EvaluationService"]
