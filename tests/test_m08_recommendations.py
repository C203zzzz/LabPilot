from backend.domain.evaluations import CandidateEvaluation, EvaluationStatus, ConstraintEvaluation, ConstraintEvaluationStatus
from backend.domain.facts import FactEvidence, FactEvidenceLevel, FactStatus
from backend.services.fact_service import make_fact
from backend.services.recommendation_service import rank_candidates
from datetime import datetime, timezone

def ev(cid, eid, source="official", level=FactEvidenceLevel.PRIMARY, authority=90, **kw):
    return FactEvidence(eid,cid,source,authority,level,datetime.now(timezone.utc),document_id="d",page=1,**kw)
def fact(cid,name,value,fid,evidence,status=FactStatus.CONFIRMED):
    return make_fact(cid,name,value,"number" if isinstance(value,(int,float)) else "text",evidence,fact_id=fid,status=status)
def evaluation(cid,status="eligible", soft=None):
    return CandidateEvaluation(cid,EvaluationStatus(status),status=="eligible",[],soft or [],"x")
def soft(name,status,fid="f",eid="e"):
    return ConstraintEvaluation(name,"soft",ConstraintEvaluationStatus(status),"r",[fid],[eid])

def test_eligible_soft_and_evidence_sort_stable():
    es=[evaluation("b",soft=[soft("speed","satisfied")]),evaluation("a",soft=[soft("speed","unknown")])]
    fs=[fact("b","price",100,"fb",ev("b","eb")),fact("a","price",100,"fa",ev("a","ea"))]
    out=rank_candidates(es,fs); assert out.preferred.candidate_id=="b"; assert out.to_dict()["status"]=="recommendations"
    assert rank_candidates(list(reversed(es)),fs).preferred.candidate_id=="b"

def test_statuses_are_separated_and_no_recommendation():
    out=rank_candidates([evaluation("r","needs_review"),evaluation("i","ineligible")],[])
    assert out.preferred is None and out.status.value=="no_recommendation"
    assert [x.candidate_id for x in out.needs_review]==["r"] and [x.candidate_id for x in out.ineligible]==["i"]

def test_historical_price_keeps_semantics_and_missing_evidence_is_penalized():
    f=fact("c","price",100,"f",ev("c","e",source="tender",level=FactEvidenceLevel.HISTORICAL,price_evidence_level="historical_tender",tender_title="2024采购结果"))
    out=rank_candidates([evaluation("c")],[f]); dims={d["name"]:d for d in out.preferred.to_dict()["dimensions"]}
    assert "历史招标价" in dims["procurement_quality"]["reason"] and dims["procurement_quality"]["missing_reason"]=="historical_price_only"

def test_invalid_duplicate_is_rejected():
    try: rank_candidates([evaluation("x"),evaluation("x")],[])
    except Exception as e: assert getattr(e,"code",None)=="invalid_evaluation"
    else: raise AssertionError("expected validation error")
