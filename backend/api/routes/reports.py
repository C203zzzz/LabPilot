"""Evaluation, recommendation, supplier follow-up, facts, reports, and history endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.api.routes.context import RouteContext
from backend.api.routes.schemas import (
    ComparisonRequest,
    EvaluationRequest,
    FactDraftCreate,
    FactReviewRequest,
    FinalDecisionRequest,
    QuestionsRequest,
    RankingRequest,
    ReevaluateRequest,
    ReportBatchRequest,
    ReportCreate,
    SupplierInquiriesCreate,
    SupplierReplyCreate,
    SupplierReplyReview,
    WebFactDraftCreate,
)
from backend.domain.errors import ModuleValidationError
from backend.domain.facts import FactEvidence, FactEvidenceLevel, FactStatus
from backend.domain.quotes import SupplierQuote
from backend.domain.supplier_followup import FinalDecisionStatus, contract_to_dict
from backend.schemas.facts import fact_to_dict
from backend.services.evaluation_service import evaluate_candidates
from backend.services.fact_service import make_fact
from backend.services.question_service import generate_question_list
from backend.services.quote_service import compare_quotes
from backend.services.recommendation_service import rank_candidates
from backend.services.report_library import export_reports, library_items
from backend.services.report_service import ReportService
from backend.services.supplier_followup_service import (
    create_inquiry,
    create_reply,
    decide_task,
    make_pending_decision,
    reply_from_dict,
    reply_to_fact_draft,
    review_fact,
    review_reply,
)


def register_report_routes(router: APIRouter, ctx: RouteContext) -> None:
    repo = ctx.repo

    def selected_reports(body: ReportBatchRequest):
        ids = set(body.report_ids)
        items = [item for item in library_items(repo) if item["id"] in ids]
        if len(items) != len(ids):
            raise HTTPException(409, detail={"code": "report_selection_stale", "message": "部分报告已不存在，请刷新列表后重新选择"})
        return items

    @router.get("/reports")
    def report_library():
        return {"items": library_items(repo)}

    @router.get("/reports/{report_id}/diff")
    def report_version_diff(report_id: str):
        from backend.services.report_diff import report_diff
        return report_diff(repo, report_id)

    @router.post("/reports/export")
    def export_report_batch(body: ReportBatchRequest):
        items = selected_reports(body)
        records = {item["id"]: repo.get("reports", item["id"]) for item in items}
        return Response(
            export_reports(items, records, Path(repo.path).parent / "research-snapshots"),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="labpilot-reports.zip"'},
        )

    @router.get("/reports/{report_id}/export")
    def export_single_report(
        report_id: str,
        format: str = Query("docx", pattern="^(docx|xlsx|zip)$"),
    ):
        record = repo.get("reports", report_id)
        if not record or not isinstance(record.get("report"), dict):
            raise HTTPException(404, detail={"code": "report_not_found", "message": "报告不存在", "field": "report_id"})
        item = next((entry for entry in library_items(repo) if entry["id"] == report_id), None)
        if item is None:
            rep = record["report"]
            item = {
                "id": report_id,
                "task_id": record.get("task_id") or "",
                "title": rep.get("title") or "采购比选报告",
                "version": rep.get("baseline_version") or 1,
                "created_at": rep.get("created_at") or "",
                "scenario": "",
            }
        if format == "zip":
            return Response(
                export_reports([item], {report_id: record}, Path(repo.path).parent / "research-snapshots"),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="labpilot-{report_id}.zip"'},
            )
        from backend.services.report_export_office import (
            export_report_docx,
            export_report_xlsx,
            office_download_headers,
            office_media_type,
        )
        content = export_report_docx(item, record) if format == "docx" else export_report_xlsx(item, record)
        return Response(
            content,
            media_type=office_media_type(format),
            headers=office_download_headers(item["title"], format),
        )

    @router.post("/reports/delete")
    def delete_report_batch(body: ReportBatchRequest):
        items = selected_reports(body)
        repo.delete_reports([item["id"] for item in items])
        return {"deleted_count": len(items)}

    @router.post("/tasks/{task_id}/evaluate")
    def evaluate_task(task_id: str, body: EvaluationRequest):
        ctx.task_or_404(task_id)
        result = {"items": [x.to_dict() for x in evaluate_candidates(body.requirement, body.candidates, body.facts)]}
        ctx.persist_workflow_state(task_id, evaluations=result["items"])
        return result

    @router.post("/tasks/{task_id}/recommend")
    def recommend_task(task_id: str, body: RankingRequest):
        ctx.task_or_404(task_id)
        result = rank_candidates(body.evaluations, body.facts, weights=body.weights).to_dict()
        ctx.persist_workflow_state(task_id, evaluations=body.evaluations, ranking=result)
        return result

    @router.post("/tasks/{task_id}/compare")
    def compare_task(task_id: str, body: ComparisonRequest):
        ctx.task_or_404(task_id)
        quotes = [SupplierQuote(**q) for q in body.quotes]
        result = compare_quotes(quotes, candidate_id=body.candidate_id).to_dict()
        ctx.persist_workflow_state(task_id, quotes=body.quotes, comparison=result)
        return result

    @router.post("/tasks/{task_id}/questions")
    def questions_task(task_id: str, body: QuestionsRequest):
        ctx.task_or_404(task_id)
        result = generate_question_list(body.evaluations, body.facts, body.quotes, body.comparisons, body.recommendations).to_dict()
        ctx.persist_workflow_state(
            task_id,
            evaluations=body.evaluations,
            quotes=body.quotes,
            comparison=body.comparisons[-1] if body.comparisons else ctx.workflow_state(task_id).get("comparison"),
            ranking=body.recommendations if isinstance(body.recommendations, dict) else ctx.workflow_state(task_id).get("ranking"),
            generated_questions=result,
        )
        return result

    @router.get("/tasks/{task_id}/final-decision")
    def get_final_decision(task_id: str):
        ctx.task_or_404(task_id)
        values = repo.list("final_decisions", task_id)
        return values[0] if values else contract_to_dict(make_pending_decision(task_id))

    @router.put("/tasks/{task_id}/final-decision")
    def put_final_decision(task_id: str, body: FinalDecisionRequest):
        ctx.task_or_404(task_id)
        value = decide_task(
            task_id,
            body.status,
            explicit_user_confirmation=body.explicit_user_confirmation,
            decided_by=body.decided_by,
            note=body.note,
        )
        payload = contract_to_dict(value)
        repo.put("final_decisions", payload["decision_id"], payload, task_id=task_id)
        return payload

    @router.post("/tasks/{task_id}/supplier-inquiries", status_code=201)
    def create_supplier_inquiries(task_id: str, body: SupplierInquiriesCreate):
        ctx.task_or_404(task_id)
        decisions = repo.list("final_decisions", task_id)
        if not decisions or decisions[0].get("status") != FinalDecisionStatus.CONFIRMED.value:
            raise HTTPException(409, detail={
                "code": "final_decision_not_confirmed",
                "message": "请先由用户显式确认最终决定",
                "field": "final_decision",
            })
        candidate_ids = {item.get("id") for item in repo.list("candidates", task_id)}
        missing = [item.candidate_id for item in body.items if item.candidate_id not in candidate_ids]
        if missing:
            raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_id"})
        output = []
        for item in body.items:
            value = create_inquiry(task_id, item.model_dump())
            payload = contract_to_dict(value)
            repo.put("supplier_inquiries", payload["inquiry_id"], payload, task_id=task_id)
            output.append(payload)
        return {"items": output}

    @router.get("/tasks/{task_id}/supplier-inquiries")
    def list_supplier_inquiries(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": repo.list("supplier_inquiries", task_id)}

    @router.post("/tasks/{task_id}/supplier-inquiries/{inquiry_id}/replies", status_code=201)
    def create_supplier_reply(task_id: str, inquiry_id: str, body: SupplierReplyCreate):
        ctx.task_or_404(task_id)
        inquiry = ctx.scoped_item_or_404("supplier_inquiries", task_id, inquiry_id, "inquiry_not_found")
        value = create_reply(task_id, inquiry, body.model_dump(mode="json"))
        payload = contract_to_dict(value)
        repo.put("supplier_replies", payload["reply_id"], payload, task_id=task_id)
        inquiry = {**inquiry, "status": "answered"}
        repo.put("supplier_inquiries", inquiry_id, inquiry, task_id=task_id)
        return payload

    @router.get("/tasks/{task_id}/supplier-replies")
    def list_supplier_replies(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": repo.list("supplier_replies", task_id)}

    @router.post("/tasks/{task_id}/supplier-replies/{reply_id}/review")
    def review_supplier_reply(task_id: str, reply_id: str, body: SupplierReplyReview):
        ctx.task_or_404(task_id)
        raw = ctx.scoped_item_or_404("supplier_replies", task_id, reply_id, "reply_not_found")
        value = review_reply(
            reply_from_dict(raw),
            body.status,
            explicit_user_confirmation=body.explicit_user_confirmation,
            reviewed_by=body.reviewed_by,
            note=body.note,
        )
        payload = contract_to_dict(value)
        repo.put("supplier_replies", reply_id, payload, task_id=task_id)
        return payload

    @router.post("/tasks/{task_id}/supplier-replies/{reply_id}/fact-drafts", status_code=201)
    def create_fact_draft_from_reply(task_id: str, reply_id: str, body: FactDraftCreate):
        ctx.task_or_404(task_id)
        raw = ctx.scoped_item_or_404("supplier_replies", task_id, reply_id, "reply_not_found")
        candidate_ids = {item.get("id") for item in repo.list("candidates", task_id)}
        if body.candidate_id not in candidate_ids:
            raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_id"})
        fact = reply_to_fact_draft(
            reply_from_dict(raw),
            candidate_id=body.candidate_id,
            field_name=body.field_name,
            value=body.value,
            value_type=body.value_type,
            unit=body.unit,
        )
        payload = fact_to_dict(fact)
        payload["task_id"] = task_id
        payload["source_reply_id"] = reply_id
        repo.put("facts", payload["fact_id"], payload, task_id=task_id)
        return payload

    @router.get("/tasks/{task_id}/facts")
    def list_task_facts(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": repo.list("facts", task_id)}

    @router.post("/tasks/{task_id}/sources/fact-drafts", status_code=201)
    def create_web_fact(task_id: str, body: WebFactDraftCreate):
        ctx.task_or_404(task_id)
        if not any(item.get("id") == body.candidate_id for item in repo.list("candidates", task_id)):
            raise HTTPException(404, detail={"code": "candidate_not_found", "message": "对应产品不存在"})
        confirmed = {item.get("url") for item in repo.list("source_decisions", task_id) if item.get("status") == "confirmed"}
        source = next(
            (
                source
                for run in repo.list("search_runs", task_id)
                for source in run.get("sources", [])
                if source.get("url") == body.url
                and source.get("body_status") == "fetched"
                and source.get("source_type") != "search_snippet"
                and body.excerpt in (source.get("body_text") or "")
            ),
            None,
        )
        if body.url not in confirmed or source is None:
            raise HTTPException(409, detail={"code": "confirmed_source_required", "message": "请确认来源，并从已读取的正文中复制原文作为依据"})
        if str(body.value).strip() not in body.excerpt:
            raise HTTPException(422, detail={"code": "unsupported_fact", "message": "参数值必须能在所选原文中找到"})
        evidence = FactEvidence(
            "evidence-" + uuid.uuid4().hex[:12],
            body.candidate_id,
            source.get("source_type") or "unknown",
            source.get("authority_score", 0),
            FactEvidenceLevel.SECONDARY,
            datetime.now(timezone.utc),
            url=body.url,
            body_location="网页正文",
            quote=body.excerpt,
        )
        fact = make_fact(body.candidate_id, body.field_name, body.value, body.value_type, [evidence], unit=body.unit, status=FactStatus.PENDING_REVIEW)
        payload = fact_to_dict(fact)
        payload["task_id"] = task_id
        repo.put("facts", fact.fact_id, payload, task_id=task_id)
        return payload

    @router.post("/tasks/{task_id}/facts/{fact_id}/review")
    def review_task_fact(task_id: str, fact_id: str, body: FactReviewRequest):
        ctx.task_or_404(task_id)
        raw = ctx.scoped_item_or_404("facts", task_id, fact_id, "fact_not_found")
        try:
            reviewed = review_fact(raw, body.action, body.reviewer_id, body.note)
        except (ModuleValidationError, ValueError) as exc:
            raise HTTPException(422, detail={"code": getattr(exc, "code", "invalid_fact_review"), "message": str(exc), "field": "action"}) from exc
        payload = fact_to_dict(reviewed)
        payload["task_id"] = task_id
        if raw.get("source_reply_id"):
            payload["source_reply_id"] = raw["source_reply_id"]
        repo.put("facts", fact_id, payload, task_id=task_id)
        return payload

    @router.post("/tasks/{task_id}/reevaluate")
    def reevaluate_task(task_id: str, body: ReevaluateRequest = ReevaluateRequest()):
        ctx.task_or_404(task_id)
        requirement = repo.get("requirements", task_id)
        candidates = repo.list("candidates", task_id)
        if body.candidate_ids:
            requested = set(body.candidate_ids)
            candidates = [item for item in candidates if item.get("id") in requested]
            if len(candidates) != len(requested):
                raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_ids"})
        facts = repo.list("facts", task_id)
        return {
            "items": [item.to_dict() for item in evaluate_candidates(requirement, candidates, facts)],
            "fact_count": len(facts),
            "pending_fact_count": sum(item.get("status") == "pending_review" for item in facts),
        }

    @router.post("/tasks/{task_id}/reports")
    def create_report(task_id: str, body: ReportCreate):
        ctx.task_or_404(task_id)
        service = ReportService()
        snapshot = service.create_version(
            task_id,
            body.requirement,
            body.candidates,
            body.evaluations,
            body.facts,
            body.quotes,
            body.comparisons,
            body.questions,
            body.ranking,
        )
        latest_version = ctx.history_service.latest(task_id)
        expected_parent = body.expected_parent_version_id or (latest_version.version_id if latest_version else None)
        history_version = ctx.history_service.create_version(
            task_id,
            body.requirement,
            body.candidates,
            snapshot,
            revision_reason=body.revision_reason,
            expected_parent_version_id=expected_parent,
        )
        report = snapshot.report
        result = service.export(snapshot, body.format)
        payload = {"report": report.to_dict(), "export": result.to_dict(), "history_version": history_version.to_dict()}
        repo.put("reports", report.report_id, payload, task_id=task_id)
        ctx.sync_knowledge_task(task_id)
        if body.format == "json":
            return payload
        if body.format in {"md", "markdown"}:
            return Response(
                result.content,
                media_type="text/markdown",
                headers={"Content-Disposition": f"attachment; filename={result.filename}"},
            )
        return payload

    @router.get("/tasks/{task_id}/history")
    def list_history(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": [item.to_dict() for item in ctx.history_service.list_versions(task_id)]}

    @router.get("/tasks/{task_id}/history/diff")
    def diff_history(task_id: str, from_version: int, to_version: int | None = None):
        ctx.task_or_404(task_id)
        return ctx.history_service.diff(task_id, from_version, to_version).to_dict()

    @router.get("/tasks/{task_id}/history/{version_number}")
    def get_history(task_id: str, version_number: int):
        ctx.task_or_404(task_id)
        value = ctx.history_service.get_version(task_id, version_number)
        if value is None:
            raise HTTPException(404, detail={"code": "version_not_found", "message": "历史版本不存在", "field": "version_number"})
        return value.to_dict()

    @router.get("/tasks/{task_id}/reports")
    def list_reports(task_id: str):
        ctx.task_or_404(task_id)
        from backend.services.procurement_research import sanitize_report
        items = [item for item in repo.list("reports", task_id) if isinstance(item, dict) and item.get("report")]
        for item in items:
            item["report"] = sanitize_report(item["report"], item.get("snapshot") or {})
        return {"items": sorted(items, key=lambda item: item.get("history_version", {}).get("version_number", 0), reverse=True)}
