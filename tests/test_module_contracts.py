from pathlib import Path

from backend.domain.errors import ModuleValidationError
from backend.services.candidate_service import extract_candidates, pages_from_document
from backend.services.document_ingestion import DocumentIngestionService
from backend.services.requirement_service import RequirementService, RequirementValidationError


def test_m01_invalid_confirm_has_structured_error():
    try:
        RequirementService().confirm({"scenario": "采购设备", "hard_constraints": [{"bad": True}]})
    except ModuleValidationError as exc:
        assert exc.code == "invalid_constraint"
        assert exc.field == "hard_constraints"
    else:
        raise AssertionError("invalid constraint should be rejected")


def test_m01_contract_round_trip_is_json_shape():
    value = RequirementService().extract("预算 2 万元，每天处理 30 个样品").to_dict()
    restored = RequirementService().confirm(value)
    assert restored.status.value == "confirmed"
    assert restored.to_dict()["scenario"] == value["scenario"]


def test_m02_to_m03_adapter_preserves_document_identity(tmp_path: Path):
    source = tmp_path / "quote.txt"
    source.write_text("赛默飞 INFINITE 200 PRO 多功能微孔板检测仪", encoding="utf-8")
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)
    candidates = extract_candidates(pages_from_document(record))
    assert record.pages[0].document_id == record.id
    assert candidates
    assert candidates[0].sources[0].document_id == record.id
