"""DeepSeek document analysis, progress/cancel, and candidate/field decision endpoints."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException

from backend.api.routes.context import RouteContext, analysis_fact, deepseek_client_cls, document_record
from backend.api.routes.schemas import AnalysisDecision, DeepSeekAnalysisRequest
from backend.infrastructure.deepseek import DeepSeekError
from backend.schemas.documents import DocumentPage, DocumentRecord
from backend.schemas.facts import fact_to_dict
from backend.services.deepseek_analysis import AnalysisValidationError, DeepSeekAnalysisService


#: 视觉失败码 → 一句给人看的话。异常类名、原文与出错行不进这里（设计规范）。
_VISION_FAILURE_REASONS = {
    "vision_invalid_response": "视觉服务返回的内容不是有效 JSON",
    "vision_output_truncated": "视觉服务输出被长度限制截断",
    "vision_provider_timeout": "视觉服务超时",
    "vision_provider_unavailable": "视觉服务暂时不可用",
    "vision_provider_not_configured": "视觉服务没有配置好",
    "vision_auth_failed": "视觉服务鉴权失败",
    "vision_input_unsupported": "视觉服务不接受这份页面图像",
    "vision_input_invalid": "页面图像是空的",
    "vision_empty_result": "视觉服务返回空结果",
    "vision_probe_failed": "视觉服务未能读回页面内容",
}

_TRUSTED_PAGE_TEXT_LABELS = {"reliable", "risky"}
_LOCAL_RECOVERY_PROVENANCE = {"structured_pdf", "local_text_layer"}


def _completed_rendered_pages(raw: Mapping[str, Any]) -> set[int]:
    extraction = raw.get("model_extraction", {})
    extraction_fields = extraction.get("fields", {}) if isinstance(extraction, Mapping) else {}

    def has_evidence(image: Mapping[str, Any], location: str) -> bool:
        if str(image.get("ocr_text", "")).strip() or image.get("structured_fields"):
            return True
        value = extraction_fields.get(location) if isinstance(extraction_fields, Mapping) else None
        return bool(isinstance(value, Mapping) and (str(value.get("text", "")).strip() or value.get("fields")))

    return {
        int(match.group(1))
        for image in raw.get("images", [])
        if isinstance(image, Mapping)
        and image.get("ocr_status") == "completed"
        and (match := re.fullmatch(r"page:(\d+)/rendered", str(image.get("source_location", ""))))
        and has_evidence(image, str(image.get("source_location", "")))
    }


def _vision_failure_reason(code: Any) -> str:
    return _VISION_FAILURE_REASONS.get(str(code), "视觉服务调用失败")


def _visual_failure_records(raw: Mapping[str, Any], pages: list[int]) -> list[dict[str, Any]]:
    from backend.services.procurement_research import redact_error_text
    images = raw.get("images", [])
    warnings = raw.get("warnings", [])
    records = []
    for page in pages:
        location = f"page:{page}/rendered"
        image = next(
            (item for item in images if isinstance(item, Mapping) and item.get("source_location") == location),
            None,
        )
        failure = image.get("failure") if isinstance(image, Mapping) else None
        if isinstance(failure, Mapping):
            records.append({**failure, "page": page})
            continue
        warning = next(
            (item for item in warnings if isinstance(item, Mapping) and item.get("location") == location),
            None,
        )
        records.append({
            "stage": "pdf_vision",
            "type": "UnknownError",
            "code": str((warning or {}).get("code") or "vision_adapter_failed"),
            "location": None,
            "source_location": location,
            "page": page,
            "error": redact_error_text(str((warning or {}).get("message") or "")),
            "note": "旧记录只有警示文本，没有异常对象、类名与出错行",
        })
    return records


def _has_local_recovered_text(page: DocumentPage) -> bool:
    return any(
        str(item.get("kind") or "") in _LOCAL_RECOVERY_PROVENANCE
        for item in (page.provenance or [])
        if isinstance(item, Mapping)
    )


def _page_has_usable_text(page: DocumentPage) -> bool:
    if not str(page.text or "").strip():
        return False
    if str((page.quality or {}).get("label") or "") in _TRUSTED_PAGE_TEXT_LABELS:
        return True
    return _has_local_recovered_text(page)


def _has_usable_body_text(record: DocumentRecord, completed_pages: set[int]) -> bool:
    if completed_pages:
        return True
    return any(_page_has_usable_text(page) for page in record.pages)


def _visual_degradation_gap(
    record: DocumentRecord,
    failed_pages: list[int],
    completed_pages: set[int],
    codes: Mapping[int, str],
) -> str:
    name = record.source_name or record.source_uri or "（未命名资料）"
    by_number = {page.number: page for page in record.pages}
    parts = []
    for page in failed_pages:
        reason = _vision_failure_reason(codes.get(page))
        entry = by_number.get(page)
        text = str((entry.text if entry is not None else "") or "")
        if entry is not None and _page_has_usable_text(entry):
            source = "本地识别的文字" if _has_local_recovered_text(entry) else "原生文字层"
            parts.append(
                f"第 {page} 页视觉识别失败（{reason}），该页改用{source}继续分析（{len(text.strip())} 字）"
            )
        elif text.strip():
            parts.append(
                f"第 {page} 页视觉识别失败（{reason}），该页原生文字不足采信"
                f"（{len(text.strip())} 字），这部分内容本轮没有可靠读到"
            )
        else:
            parts.append(
                f"第 {page} 页视觉识别失败（{reason}），该页没有任何可用正文，"
                "这部分内容本轮没有读到"
            )
    return (
        f"{name}：{len(failed_pages)} 页页面识别（OCR）失败，其余 {len(completed_pages)} 页识别完成；"
        "本次资料分析按已读到的页面继续（结果按部分资料标注）。"
        + "；".join(parts) + "。"
    )


def register_analysis_routes(router: APIRouter, ctx: RouteContext) -> None:
    repo = ctx.repo

    def _profile_legacy_pdf(raw: dict[str, Any]) -> DocumentRecord:
        try:
            record = document_record(raw)
            return ctx.ingestion.rebuild_pdf_profile(record)
        except Exception as exc:
            raise DeepSeekError(
                "pdf_profile_rebuild_failed",
                "PDF 页面质量画像重建失败，无法安全继续分析",
            ) from exc

    @router.post("/tasks/{task_id}/analysis/deepseek")
    def analyze_documents(task_id: str, body: DeepSeekAnalysisRequest = DeepSeekAnalysisRequest()):
        task = ctx.task_or_404(task_id)
        from backend.services.analysis_checkpoint import AnalysisCheckpoint, current_checkpoint
        from backend.services.procurement_research import confirmed_model
        checkpoint = AnalysisCheckpoint(repo, task_id)
        lease = repo.claim_analysis(task_id)
        if not lease:
            raise HTTPException(409, detail="资料分析正在进行，请查看进度或先停止")
        checkpoint.owner = lease
        from backend.infrastructure.lease_heartbeat import LeaseHeartbeat
        heartbeat = LeaseHeartbeat(repo, task_id, lease)
        checkpoint.set_control(cancelled=False, status="running")
        checkpoint_token = current_checkpoint.set(checkpoint)
        from backend.infrastructure.call_budget import active_budget
        budget_token = active_budget.set({"used": 0, "limit": 12})
        client_cls = deepseek_client_cls()
        try:
            client = ctx.runtime_provider_client
            if client is not None and (body.model or body.timeout):
                client = client_cls(
                    api_key=client.api_key,
                    endpoint=client.endpoint,
                    model=body.model or client.model,
                    timeout=body.timeout or client.timeout,
                    max_tokens=client.max_tokens,
                    json_retries=client.json_retries,
                    web_tool_type=client.web_tool_type,
                    transport=client.transport,
                )
            elif client is None and (body.model or body.timeout):
                client = client_cls.from_env(model=body.model or "deepseek-flash", timeout=body.timeout or 30.0)
            documents = repo.list("documents", task_id)
            vision_adapter = ctx.runtime_vision_adapter()
            visual_failures: list[dict[str, Any]] = []
            visual_gaps: list[str] = []
            if any(d.get("document_type") == "pdf" for d in documents):
                refreshed_documents = []
                for raw in documents:
                    checkpoint.check()
                    if raw.get("document_type") != "pdf":
                        refreshed_documents.append(raw)
                        continue
                    existing = document_record(raw)
                    updated = (
                        existing
                        if ctx.ingestion.has_current_pdf_profile(existing)
                        else _profile_legacy_pdf(raw)
                    )
                    if any(w.code == "pdf_parse_failed" for w in updated.warnings):
                        repo.put("documents", raw["id"], updated.to_dict(), task_id=task_id)
                        raise DeepSeekError(
                            "document_parse_incomplete",
                            "PDF页清单尚未完整解析，已保留原件；请恢复解析后继续，不能把部分页面当成整份资料",
                        )
                    profiled_payload = updated.to_dict()
                    required_pages = ctx.ingestion.required_vision_pages(updated)
                    completed_pages = _completed_rendered_pages(profiled_payload)
                    missing_pages = required_pages - completed_pages
                    if missing_pages and vision_adapter is not None:
                        updated = ctx.ingestion.analyze_pdf_pages(updated, missing_pages, vision_adapter)
                    updated.source_name = raw.get("source_name", updated.source_name)
                    updated.source_uri = raw.get("source_uri", updated.source_uri)
                    updated.category = raw.get("category", "other")
                    payload = updated.to_dict()
                    required_after = ctx.ingestion.required_vision_pages(updated)
                    completed_after = _completed_rendered_pages(payload)
                    repo.put("documents", raw["id"], payload, task_id=task_id)
                    failed_pages = sorted(required_after - completed_after)
                    if failed_pages:
                        ctx.runtime_vision_verified = False
                        if not _has_usable_body_text(updated, completed_after):
                            raise DeepSeekError(
                                "visual_page_analysis_failed",
                                f"PDF 视觉分析未完成，失败页：{failed_pages[:10]}",
                            )
                        records = _visual_failure_records(payload, failed_pages)
                        visual_failures.extend(records)
                        visual_gaps.append(_visual_degradation_gap(
                            updated,
                            failed_pages,
                            completed_after,
                            {int(item["page"]): str(item.get("code") or "") for item in records},
                        ))
                    if missing_pages:
                        ctx.runtime_vision_verified = True
                    refreshed_documents.append(payload)
                documents = refreshed_documents
            result = DeepSeekAnalysisService(client, checkpoint=checkpoint).analyze(
                scenario=body.scenario or task.get("scenario", ""),
                documents=documents,
                target_model=confirmed_model(repo, task_id),
            )
            if visual_failures or visual_gaps:
                result["gaps"] = list(result.get("gaps") or []) + visual_gaps
                result["failures"] = list(result.get("failures") or []) + visual_failures
                if result["gaps"]:
                    result["status"] = "partial"
        except AnalysisValidationError as exc:
            raise HTTPException(422, detail={"code": "invalid_analysis_request", "message": str(exc), "field": "scenario"})
        except DeepSeekError as exc:
            if exc.code == "provider_payment_required":
                ctx.runtime_provider_issue = {"code": exc.code, "message": exc.message}
                ctx.runtime_vision_verified = False
            status = 402 if exc.code == "provider_payment_required" else (503 if exc.code in {"provider_not_configured", "provider_timeout", "provider_unavailable", "provider_http_error"} else 502)
            raise HTTPException(status, detail={"code": exc.code, "message": exc.message})
        finally:
            active_budget.reset(budget_token)
            current_checkpoint.reset(checkpoint_token)
            heartbeat.close()
            checkpoint.set_control(status="stopped" if checkpoint.control().get("cancelled") else "idle")
            repo.release_analysis(task_id, lease)
        ctx.runtime_provider_issue = None
        repo.put("reports", result["analysis_id"], {"kind": "deepseek_analysis", **result}, task_id=task_id)
        ctx.sync_knowledge_task(task_id)
        return result

    @router.get("/tasks/{task_id}/analysis/progress")
    def analysis_progress(task_id: str):
        ctx.task_or_404(task_id)
        from backend.services.analysis_checkpoint import AnalysisCheckpoint
        return AnalysisCheckpoint(repo, task_id).progress()

    @router.post("/tasks/{task_id}/analysis/cancel")
    def cancel_analysis(task_id: str):
        ctx.task_or_404(task_id)
        from backend.services.analysis_checkpoint import AnalysisCheckpoint
        from backend.services.structured_pdf import terminate_active_subprocesses
        checkpoint = AnalysisCheckpoint(repo, task_id)
        checkpoint.set_control(cancelled=True)
        terminate_active_subprocesses(owner_id=task_id)
        return checkpoint.progress()

    @router.get("/tasks/{task_id}/analysis/deepseek")
    def get_latest_deepseek_analysis(task_id: str):
        ctx.task_or_404(task_id)
        values = [x for x in repo.list("reports", task_id) if x.get("kind") == "deepseek_analysis"]
        if not values:
            return {"status": "not_generated", "analysis_id": None, "candidates": [], "warnings": []}
        return max(
            values,
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("analysis_id") or item.get("id") or ""),
            ),
        )

    @router.post("/tasks/{task_id}/analysis/deepseek/decisions")
    def decide_deepseek_analysis(task_id: str, body: list[AnalysisDecision]):
        ctx.task_or_404(task_id)
        if not body:
            raise HTTPException(422, detail={"code": "empty_analysis_decisions", "message": "至少需要一条确认决定", "field": "body"})
        analysis_ids = {item.analysis_id for item in body}
        reports = {x.get("id", x.get("analysis_id")): x for x in repo.list("reports", task_id) if x.get("kind") == "deepseek_analysis"}
        missing = [x for x in analysis_ids if x not in reports]
        if missing:
            raise HTTPException(404, detail={"code": "analysis_not_found", "message": "分析草稿不存在", "field": "analysis_id"})
        output = []
        for decision in body:
            report = reports[decision.analysis_id]
            candidate = next((x for x in report.get("candidates", []) if x.get("candidate_id") == decision.candidate_id), None)
            if candidate is None:
                raise HTTPException(404, detail={"code": "analysis_candidate_not_found", "message": "分析候选不存在", "field": "candidate_id"})
            reviewed_by = (decision.reviewed_by or "").strip() or "analysis-decision:unspecified"
            reviewed_at = datetime.now(timezone.utc).isoformat()
            fields = candidate.get("fields", [])
            for field_name, raw_value in decision.field_values.items():
                field = next((x for x in fields if x.get("field_name") == field_name), None)
                if field is None:
                    raise HTTPException(404, detail={"code": "analysis_field_not_found", "message": "分析字段不存在", "field": "field_values"})
                if isinstance(raw_value, str):
                    cleaned_value = raw_value.strip()
                    if not cleaned_value:
                        raise HTTPException(422, detail={"code": "invalid_analysis_field_value", "message": "参数值不能为空", "field": field_name})
                    field["value"] = cleaned_value
                elif raw_value is None:
                    raise HTTPException(422, detail={"code": "invalid_analysis_field_value", "message": "参数值不能为空", "field": field_name})
                else:
                    field["value"] = raw_value
                field["edited_by"] = reviewed_by
                field["edited_at"] = reviewed_at
                if field.get("status") in {"unknown", "conflicted", "rejected"}:
                    field["status"] = "pending_review"
            for field_name, status in decision.field_statuses.items():
                if status not in {"confirmed", "rejected", "pending_review", "unknown", "conflicted"}:
                    raise HTTPException(422, detail={"code": "invalid_analysis_field_status", "message": "字段状态不合法", "field": "field_statuses"})
                field = next((x for x in fields if x.get("field_name") == field_name), None)
                if field is None:
                    raise HTTPException(404, detail={"code": "analysis_field_not_found", "message": "分析字段不存在", "field": "field_statuses"})
                if status == "confirmed" and field.get("status") in {"unknown", "conflicted"} and field_name not in decision.field_values:
                    raise HTTPException(409, detail={"code": "analysis_field_not_confirmable", "message": "unknown/conflicted 字段不能直接确认", "field": field_name})
                field["status"] = status
                field["reviewed_by"] = reviewed_by
                field["reviewed_at"] = reviewed_at
                field["review_note"] = decision.note
            if decision.status == "confirmed":
                unresolved = [x.get("field_name") for x in fields if x.get("status") in {"conflicted", "pending_review"}]
                if unresolved:
                    raise HTTPException(409, detail={"code": "analysis_candidate_not_ready", "message": "仍有字段未完成确认", "field": "field_statuses", "unresolved": unresolved})
            candidate["status"] = decision.status
            candidate["note"] = decision.note
            candidate["reviewed_by"] = reviewed_by
            candidate["reviewed_at"] = reviewed_at
            report["status"] = "confirmed" if all(x.get("status") == "confirmed" for x in report.get("candidates", [])) else "pending_review"
            repo.put("reports", decision.analysis_id, report, task_id=task_id)
            if decision.status in {"confirmed", "rejected"}:
                existing = repo.get("candidates", decision.candidate_id)
                if existing and existing.get("task_id") not in (None, task_id):
                    raise HTTPException(404, detail={"code": "candidate_not_found", "message": "候选不存在", "field": "candidate_id"})
                payload = {
                    "id": decision.candidate_id,
                    "candidate_id": decision.candidate_id,
                    "brand": candidate.get("brand"),
                    "series": candidate.get("series"),
                    "model": candidate.get("model"),
                    "configuration": candidate.get("configuration"),
                    "status": "confirmed" if decision.status == "confirmed" else "excluded",
                    "note": decision.note,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                    "sources": [
                        {
                            "document_id": s.get("document_id") or "unknown",
                            "location": s.get("page_or_location") or "unknown",
                            "excerpt": s.get("excerpt", ""),
                            "source_type": "uploaded_document",
                        }
                        for s in candidate.get("sources", [])
                    ],
                }
                repo.put("candidates", decision.candidate_id, payload, task_id=task_id)
            if decision.status == "confirmed":
                for field in fields:
                    if field.get("status") != "confirmed":
                        continue
                    fact = analysis_fact(
                        candidate,
                        field,
                        reviewed_by=str(field.get("reviewed_by") or reviewed_by),
                        review_note=field.get("review_note") or decision.note,
                    )
                    if fact is None:
                        continue
                    fact_payload = fact_to_dict(fact)
                    fact_payload["task_id"] = task_id
                    fact_payload["source_analysis_id"] = decision.analysis_id
                    repo.put("facts", fact_payload["fact_id"], fact_payload, task_id=task_id)
            output.append(candidate)
        return {"analysis_id": next(iter(analysis_ids)), "items": output}
