import json

import pytest

from backend.domain.errors import ModuleValidationError
from backend.domain.reports import (
    EvidenceReference,
    InMemoryReportRepository,
    ReportItem,
    SQLiteReportRepository,
)
from backend.services.report_service import ReportService, export_report
from backend.schemas.reports import report_snapshot_to_dict, selection_report_to_dict


def candidate(candidate_id="c1", brand="Acme", model="X-100", configuration="std", document_id="d1", page=1):
    return {
        "id": candidate_id,
        "brand": brand,
        "series": "Series",
        "model": model,
        "configuration": configuration,
        "status": "confirmed",
        "sources": [{
            "document_id": document_id,
            "location": f"page:{page}",
            "excerpt": f"{brand} {model}",
            "source_type": "uploaded_document",
        }],
    }


def constraint(name="capacity", status="satisfied", fact_ids=(), evidence_ids=()):
    return {
        "constraint_name": name,
        "kind": "hard",
        "status": status,
        "reason": f"{name} {status}",
        "fact_ids": list(fact_ids),
        "evidence_ids": list(evidence_ids),
        "missing_reason": None if status != "unknown" else "no_confirmed_fact",
    }


def evaluation(candidate_id="c1", status="eligible", hard=None, soft=None):
    return {
        "candidate_id": candidate_id,
        "status": status,
        "eligible": status == "eligible",
        "hard_constraints": list(hard or []),
        "soft_constraints": list(soft or []),
        "reason": status,
    }


def fact(candidate_id="c1", fact_id="f1", evidence_id="e1", document_id="d1", page=1, value=12,
         field_name="capacity", price_evidence_level=None):
    evidence = {
        "evidence_id": evidence_id,
        "candidate_id": candidate_id,
        "source_type": "user_document",
        "authority_score": 90,
        "evidence_level": "user_provided",
        "document_id": document_id,
        "page": page,
        "location": f"page:{page}",
        "quote": f"{field_name} evidence",
    }
    if price_evidence_level:
        evidence["price_evidence_level"] = price_evidence_level
        evidence["tender_title"] = "2025 historical tender"
    return {
        "fact_id": fact_id,
        "candidate_id": candidate_id,
        "field_name": field_name,
        "value": value,
        "value_type": "number",
        "unit": "samples",
        "status": "confirmed",
        "evidence": [evidence],
    }


def quote(candidate_id="c1", quote_id="q1", source_type="current_supplier_quote", amount=100000):
    return {
        "quote_id": quote_id,
        "candidate_id": candidate_id,
        "supplier": "Supplier A",
        "model": "X-100",
        "configuration": {"kit": "standard"},
        "amount": amount,
        "currency": "CNY",
        "source_type": source_type,
        "source": "https://supplier.example/quote.pdf",
        "source_location": "page:2",
        "quote_date": "2026-08-01",
        "valid_until": "2026-10-01",
        "conditions": [
            {"condition_type": "tax", "value": "含税 13%", "source_location": "page:2"},
            {"condition_type": "delivery", "value": "30 days", "source_location": "page:2"},
            {"condition_type": "installation", "value": "included", "source_location": "page:2"},
            {"condition_type": "warranty", "value": "2 years", "source_location": "page:2"},
            {"condition_type": "after_sales", "value": "24h", "source_location": "page:2"},
            {"condition_type": "payment", "value": "acceptance", "source_location": "page:2"},
        ],
    }


def recommendation(candidate_id, category, fact_ids=(), evidence_ids=()):
    return {
        "candidate_id": candidate_id,
        "category": category,
        "rank": 1 if category == "preferred" else (2 if category == "alternative" else None),
        "total_score": 80.0 if category in {"preferred", "alternative"} else None,
        "dimensions": [],
        "reasons": [{"text": f"{candidate_id} {category} reason", "kind": "positive",
                     "fact_ids": list(fact_ids), "evidence_ids": list(evidence_ids),
                     "missing_reason": None}],
        "risks": [],
        "gaps": [],
    }


def base_inputs():
    requirement = {
        "scenario": "采购一台容量至少 10 个样品的培养设备",
        "task_type": "培养/恒温",
        "hard_constraints": [{"name": "capacity", "value": {"min": 10}, "kind": "hard", "source_text": "至少 10 个样品"}],
        "soft_constraints": [],
        "unknowns": [],
        "status": "confirmed",
        "version": 1,
    }
    candidates = [candidate("c1"), candidate("c2", "Beta", "Y-100", document_id="d2", page=2)]
    evaluations = [
        evaluation("c1", "eligible", hard=[constraint("capacity", "satisfied", ["f1"], ["e1"])]),
        evaluation("c2", "ineligible", hard=[constraint("capacity", "violated", ["f2"], ["e2"])]),
    ]
    facts = [
        fact("c1", "f1", "e1", "d1", 1, 12),
        fact("c2", "f2", "e2", "d2", 2, 5),
    ]
    ranking = {
        "status": "recommendations",
        "preferred": recommendation("c1", "preferred", ["f1"], ["e1"]),
        "alternatives": [],
        "needs_review": [],
        "ineligible": [recommendation("c2", "ineligible", ["f2"], ["e2"])],
        "rule_version": "m08-v1",
    }
    return requirement, candidates, evaluations, facts, ranking


def test_preferred_alternative_order_and_evidence_locations_are_rendered():
    requirement, candidates, evaluations, facts, _ = base_inputs()
    candidates.append(candidate("c3", "Gamma", "Z-100", document_id="d3", page=3))
    evaluations.append(evaluation("c3", "eligible", hard=[constraint("capacity", "satisfied", ["f3"], ["e3"])]))
    facts.append(fact("c3", "f3", "e3", "d3", 3, 20))
    ranking = {
        "status": "recommendations",
        "preferred": recommendation("c1", "preferred", ["f1"], ["e1"]),
        "alternatives": [recommendation("c3", "alternative", ["f3"], ["e3"])],
        "needs_review": [],
        "ineligible": [recommendation("c2", "ineligible", ["f2"], ["e2"])],
        "rule_version": "m08-v1",
    }
    report = ReportService().build_report("task-1", requirement, candidates, evaluations, facts, ranking=ranking)
    assert report.status.value == "recommendations"
    assert report.preferred["candidate_id"] == "c1"
    assert [item["candidate_id"] for item in report.alternatives] == ["c3"]
    assert [section.key for section in report.sections] == [
        "recommendation", "requirements", "candidates", "hard_constraints",
        "facts", "quotes", "risks", "gaps", "supplier_questions",
    ]
    markdown = export_report(report, "markdown").content
    assert markdown.index("**首选**") < markdown.index("## 采购需求")
    assert "**备选**：c3" in markdown
    assert "**淘汰原因**：c2" in markdown
    assert "document_id=d1" in markdown and "page=1" in markdown
    assert "document_id=d3" in markdown and "page=3" in markdown


def test_no_recommendation_separates_review_and_ineligible_without_preferred():
    requirement, candidates, _, facts, _ = base_inputs()
    evaluations = [
        evaluation("c1", "needs_review", hard=[constraint("capacity", "unknown", ["f1"], ["e1"])]),
        evaluation("c2", "ineligible", hard=[constraint("capacity", "violated", ["f2"], ["e2"])]),
    ]
    report = ReportService().build_report("task-2", requirement, candidates, evaluations, facts)
    assert report.status.value == "no_recommendation"
    assert report.preferred is None
    assert [item["candidate_id"] for item in report.needs_review] == ["c1"]
    assert [item["candidate_id"] for item in report.ineligible] == ["c2"]
    markdown = export_report(report, "markdown").content
    assert "无推荐（no_recommendation）" in markdown
    assert "待复核（不得作为首选）" in markdown
    assert "**淘汰原因**：c2" in markdown

def test_historical_tender_price_never_becomes_current_quote():
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    historical = quote(source_type="historical_tender_price", amount=90000)
    report = ReportService().build_report(
        "task-history", requirement, candidates, evaluations, facts, quotes=[historical], ranking=ranking,
    )
    markdown = export_report(report, "markdown").content
    assert "历史招标价（历史参考，非当前报价）" in markdown
    assert "只能作为历史参考" in markdown
    assert "当前供应商报价：c1" not in markdown
    exported = json.loads(export_report(report, "json").content)
    quote_section = next(section for section in exported["sections"] if section["key"] == "quotes")
    assert quote_section["items"][0]["text"].startswith("历史招标价")
    assert any(step["field"] == "current_quote" for step in exported["supplier_questions"])


def test_questions_and_evidence_associations_are_in_report_appendix():
    requirement, candidates, _, facts, _ = base_inputs()
    evaluations = [
        evaluation("c1", "needs_review", hard=[constraint("capacity", "unknown", ["f1"], ["e1"])]),
        evaluation("c2", "ineligible", hard=[constraint("capacity", "violated", ["f2"], ["e2"])]),
    ]
    report = ReportService().build_report("task-questions", requirement, candidates, evaluations, facts)
    assert report.supplier_questions
    assert all(question["candidate_id"] in {"c1", "c2"} for question in report.supplier_questions)
    question_section = report.section("supplier_questions")
    assert question_section is not None and question_section.items
    assert any("请书面确认" in item.text or "复核" in item.text for item in question_section.items)
    assert "供应商追问清单" in export_report(report, "markdown").content


def test_markdown_and_json_exports_are_deterministic_and_stable():
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    service = ReportService()
    report = service.build_report("task-export", requirement, candidates, evaluations, facts, ranking=ranking)
    first_md = service.export(report, "markdown")
    second_md = service.export(report, "markdown")
    first_json = service.export(report, "json")
    second_json = service.export(report, "json")
    assert first_md.content == second_md.content
    assert first_json.content == second_json.content
    assert first_md.content_hash == second_md.content_hash
    assert first_md.content.endswith("\n") and first_json.content.endswith("\n")
    payload = json.loads(first_json.content)
    assert payload == selection_report_to_dict(report)
    assert payload["status"] == "recommendations"
    assert set(payload) >= {"report_id", "task_id", "status", "preferred", "sections", "content_hash"}


def test_version_increment_idempotency_parent_and_history_query():
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    service = ReportService(InMemoryReportRepository())
    created = "2026-09-18T10:00:00+08:00"
    v1 = service.create_version(
        "task-version", requirement, candidates, evaluations, facts, ranking=ranking,
        revision_reason="initial", created_at=created,
    )
    same = service.create_version(
        "task-version", requirement, candidates, evaluations, facts, ranking=ranking,
        revision_reason="ignored when idempotent", created_at="2026-09-18T11:00:00+08:00",
    )
    assert same.version.version_id == v1.version.version_id
    assert same.version.version_number == 1
    changed = dict(requirement)
    changed["scenario"] = changed["scenario"] + "，需要增加环境记录"
    v2 = service.create_version(
        "task-version", changed, candidates, evaluations, facts, ranking=ranking,
        revision_reason="requirement changed", created_at="2026-09-19T10:00:00+08:00",
        expected_parent_version_id=v1.version.version_id,
    )
    assert v2.version.version_number == 2
    assert v2.version.parent_version_id == v1.version.version_id
    assert v2.version.revision_reason == "requirement changed"
    assert v2.version.content_hash != v1.version.content_hash
    history = service.history("task-version")
    assert [snapshot.version.version_number for snapshot in history] == [1, 2]
    assert service.get_version("task-version", 1).report.content_hash == v1.report.content_hash
    assert service.get_version("task-version", 99) is None


def test_version_conflict_and_cross_task_history_isolation():
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    repository = InMemoryReportRepository()
    service = ReportService(repository)
    v1 = service.create_version("task-a", requirement, candidates, evaluations, facts, ranking=ranking)
    changed = dict(requirement)
    changed["scenario"] = "changed scenario"
    with pytest.raises(ModuleValidationError) as caught:
        service.create_version(
            "task-a", changed, candidates, evaluations, facts, ranking=ranking,
            expected_parent_version_id="version-stale",
        )
    assert caught.value.code == "version_conflict"
    assert repository.list_versions("task-b") == []
    current = service.get_version("task-a", 1)
    current.report.preferred["candidate_id"] = "mutated"
    assert service.get_version("task-a", 1).report.preferred["candidate_id"] == "c1"
    assert v1.version.version_number == 1


def test_invalid_inputs_missing_candidate_and_unsafe_ranking_have_module_errors():
    requirement, candidates, evaluations, facts, ranking = base_inputs()
    service = ReportService()
    with pytest.raises(ModuleValidationError) as invalid_task:
        service.build_report("", requirement, candidates, evaluations, facts, ranking=ranking)
    assert invalid_task.value.code == "invalid_input" and invalid_task.value.field == "task_id"
    with pytest.raises(ModuleValidationError) as invalid_candidates:
        service.build_report("task", requirement, {}, evaluations, facts, ranking=ranking)
    assert invalid_candidates.value.code == "invalid_input" and invalid_candidates.value.field == "candidates"
    missing = [dict(facts[0])]
    missing[0]["candidate_id"] = "missing-candidate"
    with pytest.raises(ModuleValidationError) as missing_ref:
        service.build_report("task", requirement, candidates, evaluations, missing, ranking=ranking)
    assert missing_ref.value.code == "candidate_reference_missing" and missing_ref.value.field == "facts"
    unsafe = json.loads(json.dumps(ranking))
    unsafe["preferred"] = recommendation("c1", "preferred")
    unsafe["preferred"]["category"] = "preferred"
    unsafe["needs_review"] = [recommendation("c2", "needs_review")]
    unsafe["ineligible"] = []
    with pytest.raises(ModuleValidationError) as unsafe_ranking:
        service.build_report("task", requirement, candidates, evaluations, facts, ranking=unsafe)
    assert unsafe_ranking.value.code == "invalid_recommendation"
    with pytest.raises(ModuleValidationError) as invalid_export:
        service.export(service.build_report("task", requirement, candidates, evaluations, facts, ranking=ranking), "docx")
    assert invalid_export.value.code == "invalid_export_format"


def test_report_item_without_evidence_or_reason_is_rejected_and_sqlite_is_boundary_only():
    with pytest.raises(ModuleValidationError) as caught:
        ReportItem("结论", "没有证据")
    assert caught.value.code == "missing_evidence_reason"
    reference = EvidenceReference(document_id="d1", location="page:1")
    assert reference.document_id == "d1"
    repository = SQLiteReportRepository()
    with pytest.raises(NotImplementedError):
        repository.save(object())
