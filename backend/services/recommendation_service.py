"""Pure, configurable M08 ranking service."""
from __future__ import annotations

from typing import Any, Iterable
from backend.domain.errors import ModuleValidationError
from backend.domain.evaluations import ConstraintEvaluationStatus, EvaluationStatus
from backend.domain.facts import FactStatus
from backend.domain.recommendations import *

DEFAULT_WEIGHTS = {"soft_match": .45, "evidence_completeness": .25,
                   "source_authority": .15, "procurement_quality": .15}

def _get(o, k, default=None): return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)
def _val(x): return getattr(x, "value", x)
def _ids(evals):
    fs, es = [], []
    for e in evals:
        fs += list(_get(e, "fact_ids", []) or []); es += list(_get(e, "evidence_ids", []) or [])
    return sorted(set(map(str, fs))), sorted(set(map(str, es)))

def _facts_for(facts, cid): return [f for f in facts if str(_get(f, "candidate_id", "")) == cid]

def _dimension(name, score, weight, reason, evals=(), missing=None, missing_reason=None):
    if missing_reason is not None: missing = missing_reason
    fs, es = _ids(evals)
    return ScoreDimension(name, round(max(0., min(100., score)), 4), weight, reason, fs, es, missing)

def _soft(evaluation, weight):
    xs = list(_get(evaluation, "soft_constraints", []) or [])
    if not xs: return _dimension("soft_match", 100., weight, "没有可比较的软约束，未扣分", missing_reason="no_soft_constraints")
    vals = [_val(_get(x, "status")) for x in xs]
    score = 100. * (vals.count(ConstraintEvaluationStatus.SATISFIED.value) + .25 * vals.count(ConstraintEvaluationStatus.NOT_APPLICABLE.value)) / len(vals)
    unknown = vals.count(ConstraintEvaluationStatus.UNKNOWN.value)
    return _dimension("soft_match", score, weight,
                      f"软约束满足 {vals.count('satisfied')}/{len(vals)}，未知 {unknown} 项", xs,
                      "soft_constraint_unknown" if unknown else None)

def _evidence(facts, weight):
    confirmed = [f for f in facts if _val(_get(f, "status")) == FactStatus.CONFIRMED.value]
    all_ids = [str(_get(f, "fact_id")) for f in facts if _get(f, "fact_id")]
    evid = [e for f in facts for e in (_get(f, "evidence", []) or [])]
    eids = [str(_get(e, "evidence_id")) for e in evid if _get(e, "evidence_id")]
    if not facts: return ScoreDimension("evidence_completeness", 0., weight, "没有候选事实", all_ids, eids, "no_fact")
    score = 100. * len(confirmed) / len(facts)
    return ScoreDimension("evidence_completeness", score, weight,
                          f"已确认事实 {len(confirmed)}/{len(facts)}", all_ids, eids,
                          None if confirmed else "no_confirmed_fact")

def _authority(facts, weight):
    evid = [e for f in facts for e in (_get(f, "evidence", []) or [])]
    if not evid: return ScoreDimension("source_authority", 0., weight, "没有可评价的证据来源", missing_reason="no_evidence")
    scores = [float(_get(e, "authority_score", 0) or 0) for e in evid]
    score = min(100., sum(scores) / len(scores))
    ids = [str(_get(e, "evidence_id")) for e in evid if _get(e, "evidence_id")]
    return ScoreDimension("source_authority", score, weight, f"证据来源权威性均值 {score:.1f}", evidence_ids=ids)

def _procurement(facts, weight):
    prices = [e for f in facts if str(_get(f, "field_name", "")).lower() in {"price", "budget", "price_amount"}
              for e in (_get(f, "evidence", []) or [])]
    if not prices: return ScoreDimension("procurement_quality", 0., weight, "没有价格或采购条件证据", missing_reason="no_price_evidence")
    vals=[]; ids=[]; historical=False
    for e in prices:
        ids.append(str(_get(e, "evidence_id")))
        level = _get(e, "price_evidence_level") or _val(_get(e, "evidence_level"))
        if level == "historical_tender": historical=True; vals.append(45.)
        elif str(_get(e, "source_type", "")).lower() in {"official", "official_price"}: vals.append(100.)
        elif str(_get(e, "source_type", "")).lower() in {"dealer", "distributor"}: vals.append(65.)
        else: vals.append(35.)
    reason = "含历史招标价，仅作为历史参考，不能视为当前报价" if historical else "价格/采购条件来源已分级"
    return ScoreDimension("procurement_quality", sum(vals)/len(vals), weight, reason, evidence_ids=sorted(set(ids)), missing_reason="historical_price_only" if historical else None)

def _recommendation(cid, category, rank, score, dims, evaluation):
    reasons=[]; risks=[]; gaps=[]
    for d in dims:
        target = reasons if d.score >= 70 else risks
        target.append(RecommendationReason(f"{d.name}: {d.reason}", "positive" if d.score >= 70 else "risk", d.fact_ids, d.evidence_ids, d.missing_reason))
        if d.missing_reason: gaps.append(RecommendationReason(f"{d.name} 缺口: {d.missing_reason}", "gap", d.fact_ids, d.evidence_ids, d.missing_reason))
    if _val(_get(evaluation, "status")) == EvaluationStatus.INELIGIBLE.value:
        for c in _get(evaluation, "hard_constraints", []) or []:
            if _val(_get(c, "status")) == ConstraintEvaluationStatus.VIOLATED.value:
                risks.append(RecommendationReason(_get(c,"reason", "违反硬约束"), "hard_constraint", list(_get(c,"fact_ids",[]) or []), list(_get(c,"evidence_ids",[]) or [])))
    if _val(_get(evaluation, "status")) == EvaluationStatus.NEEDS_REVIEW.value:
        for c in _get(evaluation, "hard_constraints", []) or []:
            if _val(_get(c, "status")) == ConstraintEvaluationStatus.UNKNOWN.value:
                reason = _get(c, "reason", "硬约束无法确认")
                refs_f=list(_get(c,"fact_ids",[]) or []); refs_e=list(_get(c,"evidence_ids",[]) or [])
                risks.append(RecommendationReason(reason, "needs_review", refs_f, refs_e, _get(c,"missing_reason")))
                gaps.append(RecommendationReason(f"待确认硬约束：{_get(c,'constraint_name','未命名')}", "gap", refs_f, refs_e, _get(c,"missing_reason")))
    return Recommendation(cid, category, rank, round(score, 4) if score is not None else None, dims, reasons, risks, gaps)

def rank_candidates(evaluations: Iterable[Any], facts: Iterable[Any], *, weights: dict[str,float] | None = None) -> RankingResult:
    """Rank only eligible evaluations; needs_review/ineligible remain separate."""
    if not isinstance(evaluations, (list, tuple)): raise ModuleValidationError("evaluations 必须是列表", code="invalid_evaluations", field="evaluations")
    if not isinstance(facts, (list, tuple)): raise ModuleValidationError("facts 必须是列表", code="invalid_facts", field="facts")
    evs=list(evaluations); fs=list(facts)
    w=dict(DEFAULT_WEIGHTS); w.update(weights or {})
    if set(w) != set(DEFAULT_WEIGHTS) or any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise ModuleValidationError("weights 必须包含固定维度且为非负数", code="invalid_weights", field="weights")
    seen=set(); scored=[]; review=[]; bad=[]
    for e in evs:
        cid=str(_get(e,"candidate_id", ""))
        if not cid or cid in seen: raise ModuleValidationError("候选评估缺少或重复 candidate_id", code="invalid_evaluation", field="evaluations")
        seen.add(cid); status=_val(_get(e,"status")); cfs=_facts_for(fs,cid)
        if status == EvaluationStatus.ELIGIBLE.value:
            dims=[_soft(e,w["soft_match"]),_evidence(cfs,w["evidence_completeness"]),_authority(cfs,w["source_authority"]),_procurement(cfs,w["procurement_quality"])]
            score=sum(d.score*d.weight for d in dims)/sum(d.weight for d in dims)
            scored.append((cid,score,dims,e))
        elif status == EvaluationStatus.NEEDS_REVIEW.value:
            review.append(_recommendation(cid,RecommendationCategory.NEEDS_REVIEW,None,None,[],e))
        elif status == EvaluationStatus.INELIGIBLE.value:
            bad.append(_recommendation(cid,RecommendationCategory.INELIGIBLE,None,None,[],e))
        else: raise ModuleValidationError("未知 CandidateEvaluation 状态", code="invalid_evaluation", field="status")
    scored.sort(key=lambda x:(-x[1], x[0]))
    preferred=None; alternatives=[]
    for i,(cid,score,dims,e) in enumerate(scored):
        r=_recommendation(cid, RecommendationCategory.PREFERRED if i==0 else RecommendationCategory.ALTERNATIVE, i+1, score,dims,e)
        if i==0: preferred=r
        else: alternatives.append(r)
    review.sort(key=lambda x:x.candidate_id); bad.sort(key=lambda x:x.candidate_id)
    return RankingResult(RankingStatus.RECOMMENDATIONS if preferred else RankingStatus.NO_RECOMMENDATION, preferred, alternatives, review, bad)

class RecommendationService:
    rank = staticmethod(rank_candidates)
    recommend = staticmethod(rank_candidates)

rank_recommendations = rank_candidates

__all__=["rank_candidates","rank_recommendations","RecommendationService","DEFAULT_WEIGHTS"]
