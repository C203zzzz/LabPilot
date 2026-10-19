"""对抗性测试套件：领域模型

测试所有改进后的领域模型在极端场景下的行为
"""
import pytest
from datetime import datetime, timezone
from backend.domain.candidates_improved import (
    ProductCandidate, CandidateSource, CandidateStatus, merge_candidates
)
from backend.domain.requirements_improved import (
    RequirementSpec, Constraint, ConstraintKind, RequirementStatus
)
from backend.domain.facts_improved import (
    ProductFact, FactEvidence, FactStatus, FactValueType, FactEvidenceLevel,
    reconcile_facts
)
from backend.domain.search_improved import (
    SearchQuery, SearchResult, SourceRecord, SourceType, BodyStatus,
    PriceEvidenceLevel, normalize_url
)


class TestCandidatesAdversarial:
    """ProductCandidate对抗性测试"""

    def test_empty_model_rejected(self):
        """空型号应被拒绝"""
        with pytest.raises(ValueError, match="model cannot be empty"):
            ProductCandidate(brand="Test", series=None, model="")

    def test_whitespace_only_model_rejected(self):
        """仅空格的型号应被拒绝"""
        with pytest.raises(ValueError, match="model cannot be empty"):
            ProductCandidate(brand="Test", series=None, model="   ")

    def test_immutability_enforced(self):
        """不可变性强制执行"""
        candidate = ProductCandidate(brand="Test", series=None, model="X100")
        with pytest.raises(Exception):  # FrozenInstanceError或AttributeError
            candidate.model = "X200"

    def test_sources_immutable(self):
        """sources容器不可变"""
        candidate = ProductCandidate(brand="Test", series=None, model="X100")
        assert isinstance(candidate.sources, tuple)
        with pytest.raises(AttributeError):
            candidate.sources.append(CandidateSource("doc1", "page1", "excerpt"))

    def test_invalid_brand_type(self):
        """错误的品牌类型"""
        with pytest.raises(ValueError, match="brand must be string"):
            ProductCandidate(brand=123, series=None, model="X100")

    def test_merge_with_non_candidate_rejected(self):
        """合并非候选对象应被拒绝"""
        candidate = ProductCandidate(brand="Test", series=None, model="X100")
        with pytest.raises(ValueError, match="expected ProductCandidate"):
            merge_candidates([candidate, "not a candidate"])

    def test_status_transition_safety(self):
        """状态转换安全性"""
        candidate = ProductCandidate(brand="Test", series=None, model="X100")
        confirmed = candidate.with_status(CandidateStatus.CONFIRMED)

        # 原对象未改变
        assert candidate.status == CandidateStatus.PENDING
        # 新对象状态正确
        assert confirmed.status == CandidateStatus.CONFIRMED
        # 不是同一个对象
        assert candidate is not confirmed


class TestRequirementsAdversarial:
    """RequirementSpec对抗性测试"""

    def test_empty_scenario_rejected(self):
        """空场景应被拒绝"""
        with pytest.raises(ValueError, match="scenario cannot be empty"):
            RequirementSpec(scenario="")

    def test_negative_budget_rejected(self):
        """负预算应被拒绝"""
        with pytest.raises(ValueError, match="must be positive"):
            RequirementSpec(
                scenario="test",
                budget={"max_amount": -1000}
            )

    def test_zero_budget_rejected(self):
        """零预算应被拒绝"""
        with pytest.raises(ValueError, match="must be positive"):
            RequirementSpec(
                scenario="test",
                budget={"max_amount": 0}
            )

    def test_negative_throughput_rejected(self):
        """负通量应被拒绝"""
        with pytest.raises(ValueError, match="must be positive"):
            RequirementSpec(
                scenario="test",
                throughput={"per_day": -10}
            )

    def test_invalid_version_rejected(self):
        """无效版本号应被拒绝"""
        with pytest.raises(ValueError, match="version must be"):
            RequirementSpec(scenario="test", version=0)

        with pytest.raises(ValueError, match="version must be"):
            RequirementSpec(scenario="test", version=-1)

    def test_double_confirm_rejected(self):
        """重复确认应被拒绝"""
        spec = RequirementSpec(scenario="test")
        confirmed = spec.confirm()

        with pytest.raises(ValueError, match="already confirmed"):
            confirmed.confirm()

    def test_constraint_empty_name_rejected(self):
        """空约束名应被拒绝"""
        with pytest.raises(ValueError, match="name cannot be empty"):
            Constraint(name="", value=100, kind=ConstraintKind.HARD)

    def test_immutability_enforced(self):
        """不可变性强制执行"""
        spec = RequirementSpec(scenario="test")
        with pytest.raises(Exception):
            spec.version = 999


class TestFactsAdversarial:
    """ProductFact对抗性测试"""

    def test_empty_candidate_id_rejected(self):
        """空候选ID应被拒绝"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c1",
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        with pytest.raises(ValueError, match="candidate_id cannot be empty"):
            ProductFact(
                fact_id="f1",
                candidate_id="",
                field_name="temperature",
                value=25,
                value_type=FactValueType.NUMBER,
                unit="℃",
                evidence=(evidence,),
                status=FactStatus.EXTRACTED
            )

    def test_empty_field_name_rejected(self):
        """空字段名应被拒绝"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c1",
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        with pytest.raises(ValueError, match="field_name cannot be empty"):
            ProductFact(
                fact_id="f1",
                candidate_id="c1",
                field_name="",
                value=25,
                value_type=FactValueType.NUMBER,
                unit="℃",
                evidence=(evidence,),
                status=FactStatus.EXTRACTED
            )

    def test_no_evidence_rejected(self):
        """无证据应被拒绝"""
        with pytest.raises(ValueError, match="must have at least one evidence"):
            ProductFact(
                fact_id="f1",
                candidate_id="c1",
                field_name="temperature",
                value=25,
                value_type=FactValueType.NUMBER,
                unit="℃",
                evidence=(),
                status=FactStatus.EXTRACTED
            )

    def test_evidence_candidate_mismatch_rejected(self):
        """证据候选ID不匹配应被拒绝"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c2",  # 不同的candidate_id
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        with pytest.raises(ValueError, match="candidate_id mismatch"):
            ProductFact(
                fact_id="f1",
                candidate_id="c1",
                field_name="temperature",
                value=25,
                value_type=FactValueType.NUMBER,
                unit="℃",
                evidence=(evidence,),
                status=FactStatus.EXTRACTED
            )

    def test_invalid_authority_score_rejected(self):
        """无效权威分数应被拒绝"""
        with pytest.raises(ValueError, match="authority_score must be 0-100"):
            FactEvidence(
                evidence_id="e1",
                candidate_id="c1",
                source_type="official",
                authority_score=150,
                evidence_level=FactEvidenceLevel.PRIMARY,
                extracted_at=datetime.now(timezone.utc)
            )

    def test_negative_price_rejected(self):
        """负价格应被拒绝"""
        with pytest.raises(ValueError, match="must be non-negative"):
            FactEvidence(
                evidence_id="e1",
                candidate_id="c1",
                source_type="official",
                authority_score=90,
                evidence_level=FactEvidenceLevel.PRIMARY,
                extracted_at=datetime.now(timezone.utc),
                price_amount=-100.0
            )

    def test_illegal_state_transition_rejected_confirm(self):
        """非法状态转换应被拒绝：REJECTED -> CONFIRMED"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c1",
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        fact = ProductFact(
            fact_id="f1",
            candidate_id="c1",
            field_name="temperature",
            value=25,
            value_type=FactValueType.NUMBER,
            unit="℃",
            evidence=(evidence,),
            status=FactStatus.EXTRACTED
        )

        rejected = fact.reject(reviewer_id="reviewer1")

        with pytest.raises(ValueError, match="cannot confirm rejected fact"):
            rejected.confirm(reviewer_id="reviewer1")

    def test_illegal_state_transition_rejected_reject(self):
        """非法状态转换应被拒绝：CONFIRMED -> REJECTED"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c1",
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        fact = ProductFact(
            fact_id="f1",
            candidate_id="c1",
            field_name="temperature",
            value=25,
            value_type=FactValueType.NUMBER,
            unit="℃",
            evidence=(evidence,),
            status=FactStatus.EXTRACTED
        )

        confirmed = fact.confirm(reviewer_id="reviewer1")

        with pytest.raises(ValueError, match="cannot reject confirmed fact"):
            confirmed.reject(reviewer_id="reviewer1")

    def test_empty_reviewer_id_rejected(self):
        """空审核者ID应被拒绝"""
        evidence = FactEvidence(
            evidence_id="e1",
            candidate_id="c1",
            source_type="official",
            authority_score=90,
            evidence_level=FactEvidenceLevel.PRIMARY,
            extracted_at=datetime.now(timezone.utc)
        )

        fact = ProductFact(
            fact_id="f1",
            candidate_id="c1",
            field_name="temperature",
            value=25,
            value_type=FactValueType.NUMBER,
            unit="℃",
            evidence=(evidence,),
            status=FactStatus.EXTRACTED
        )

        with pytest.raises(ValueError, match="reviewer_id cannot be empty"):
            fact.confirm(reviewer_id="")


class TestSearchAdversarial:
    """Search模块对抗性测试"""

    def test_empty_query_text_rejected(self):
        """空查询文本应被拒绝"""
        with pytest.raises(ValueError, match="query text cannot be empty"):
            SearchQuery(text="")

    def test_query_text_too_long_rejected(self):
        """超长查询文本应被拒绝"""
        with pytest.raises(ValueError, match="query text too long"):
            SearchQuery(text="x" * 501)

    def test_invalid_limit_rejected(self):
        """无效limit应被拒绝"""
        with pytest.raises(ValueError, match="limit must be 1-100"):
            SearchQuery(text="test", limit=0)

        with pytest.raises(ValueError, match="limit must be 1-100"):
            SearchQuery(text="test", limit=101)

    def test_url_too_long_rejected(self):
        """超长URL应被拒绝"""
        long_url = "http://example.com/" + "a" * 2048
        with pytest.raises(ValueError, match="URL too long"):
            normalize_url(long_url)

    def test_invalid_url_scheme_rejected(self):
        """无效URL协议应被拒绝"""
        with pytest.raises(ValueError, match="invalid URL scheme"):
            normalize_url("ftp://example.com")

    def test_url_with_credentials_rejected(self):
        """包含认证信息的URL应被拒绝"""
        with pytest.raises(ValueError, match="credentials are not allowed"):
            normalize_url("http://user:pass@example.com")

    def test_empty_url_rejected(self):
        """空URL应被拒绝"""
        with pytest.raises(ValueError, match="invalid URL: empty"):
            normalize_url("")

    def test_body_text_too_large_rejected(self):
        """超大body_text应被拒绝"""
        with pytest.raises(ValueError, match="body_text too large"):
            SourceRecord(
                url="http://example.com",
                title="Test",
                source_type=SourceType.UNKNOWN,
                authority_score=50,
                authority_reason="test",
                body_text="x" * (65 * 1024 * 1024)  # 超过64MB
            )

    def test_invalid_authority_score_rejected(self):
        """无效权威分数应被拒绝"""
        with pytest.raises(ValueError, match="authority_score must be 0-100"):
            SourceRecord(
                url="http://example.com",
                title="Test",
                source_type=SourceType.UNKNOWN,
                authority_score=150,
                authority_reason="test"
            )

    def test_negative_price_amount_rejected(self):
        """负价格金额应被拒绝"""
        with pytest.raises(ValueError, match="must be non-negative"):
            SourceRecord(
                url="http://example.com",
                title="Test",
                source_type=SourceType.TENDER_DOCUMENT,
                authority_score=85,
                authority_reason="test",
                price_amount=-100.0
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
