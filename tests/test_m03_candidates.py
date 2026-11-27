import pytest

from backend.domain.candidates import CandidateStatus, ProductCandidate, CandidateSource, merge_candidates
from backend.services.candidate_service import ParsedPage, decide, extract_candidates, pages_from_document, confirmed_candidates
from backend.services.document_ingestion import DocumentIngestionService


def test_extraction_keeps_source_and_starts_pending():
    candidates = extract_candidates([
        ParsedPage("quote-1", "page 2", "赛默飞 KingFisher Apex series. Model APEX-2000, configuration: 96-well deck")
    ])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.brand == "赛默飞"
    assert candidate.model == "APEX-2000"
    assert candidate.configuration == "96-well deck"
    assert candidate.status is CandidateStatus.PENDING
    assert candidate.sources[0].document_id == "quote-1"
    assert candidate.sources[0].location == "page 2"


def test_same_model_with_different_configuration_is_not_merged():
    a = ProductCandidate("Acme", "X", "X-100", "standard")
    b = ProductCandidate("Acme", "X", "X-100", "96-well")
    assert len(merge_candidates([a, b])) == 2


def test_duplicate_observations_merge_sources():
    a = ProductCandidate("Acme", None, "X-100", sources=[CandidateSource("a", "p1", "X-100")])
    b = ProductCandidate("Acme", None, "X-100", sources=[CandidateSource("b", "p3", "X-100")])
    result = merge_candidates([a, b])
    assert len(result) == 1
    assert {s.document_id for s in result[0].sources} == {"a", "b"}


def test_missing_brand_merges_into_explicit_brand_and_keeps_all_sources():
    unnamed = ProductCandidate(None, None, "Infinite 200 PRO", sources=[CandidateSource("a", "p1", "title")])
    named = ProductCandidate("Tecan", None, "Infinite 200 PRO", sources=[CandidateSource("a", "p7", "body")])
    result = merge_candidates([unnamed, named])
    assert len(result) == 1
    assert result[0].brand == "Tecan"
    assert len(result[0].sources) == 2


def test_user_decision_is_explicit_and_invalid_status_rejected():
    candidate = ProductCandidate(None, None, "X-100")
    candidate = decide(candidate, CandidateStatus.CONFIRMED, "用户核对报价单")
    assert candidate.status is CandidateStatus.CONFIRMED
    assert candidate.note == "用户核对报价单"
    with pytest.raises(ValueError):
        decide(candidate, "guessed")


def test_only_confirmed_candidates_are_search_eligible():
    pending = ProductCandidate("Tecan", None, "X-100")
    excluded = ProductCandidate("Tecan", None, "X-200", status=CandidateStatus.EXCLUDED)
    confirmed = ProductCandidate("Tecan", None, "X-300", status=CandidateStatus.CONFIRMED)
    assert confirmed_candidates([pending, excluded, confirmed]) == [confirmed]


def test_conflicting_non_pending_decisions_return_to_pending():
    a = ProductCandidate("Acme", None, "X-100", status=CandidateStatus.CONFIRMED)
    b = ProductCandidate("Acme", None, "X-100", status=CandidateStatus.EXCLUDED)
    result = merge_candidates([a, b])[0]
    assert result.status is CandidateStatus.PENDING
    assert "冲突" in result.note


def test_m02_document_record_adapts_without_manual_field_mapping(tmp_path):
    source = tmp_path / "supplier.txt"
    source.write_text("品牌 Eppendorf，型号 5425-R，配置: 24-place rotor", encoding="utf-8")
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)
    pages = pages_from_document(record)
    assert pages and pages[0].document_id == record.id
    assert pages[0].location == "page:1"
    assert pages[0].source_type == "text"
    candidates = extract_candidates(pages)
    assert any(c.model == "5425-R" and c.sources[0].document_id == record.id for c in candidates)


def test_real_infinite_brochure_rejects_parameter_numbers(tmp_path):
    pdf = "data/fixtures/pdf/INFINITE 200 PRO多功能微孔板检测仪(1).pdf"
    if not __import__("pathlib").Path(pdf).exists():
        pytest.skip("真实 PDF fixture 不存在")
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(pdf)
    candidates = extract_candidates(pages_from_document(record))
    models = {c.model for c in candidates}
    assert models == {"Infinite 200 PRO"}
    assert len(candidates) == 1
    assert candidates[0].brand == "Tecan"
    for noise in {"6-384", "260/280", "EP2045015", "04-2017", "144-041", "OD 100"}:
        assert noise not in models
    assert all(c.status is CandidateStatus.PENDING for c in candidates)
