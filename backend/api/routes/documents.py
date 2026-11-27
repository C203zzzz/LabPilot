"""Document upload, listing, and explicit multimodal OCR endpoints."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from backend.api.routes.context import RouteContext, document_record, remove_task_owned_files
from backend.schemas.documents import ParseWarning


def register_document_routes(router: APIRouter, ctx: RouteContext) -> None:
    repo = ctx.repo

    @router.post("/tasks/{task_id}/documents", status_code=201)
    async def upload_document(task_id: str, file: UploadFile = File(...), category: str = Form("other")):
        ctx.task_or_404(task_id)
        if category not in {"product", "quote", "other"}:
            raise HTTPException(422, detail={"code": "invalid_document_category", "message": "资料类别不受支持", "field": "category"})
        suffix = Path(file.filename or "upload.bin").suffix
        incoming = ctx.storage_dir / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}{suffix}"
        tmp.write_bytes(await file.read())
        record = await run_in_threadpool(
            ctx.ingestion.ingest_file, tmp, vision_adapter=ctx.runtime_vision_adapter()
        )
        record.source_name = Path(file.filename or "upload.bin").name
        record.category = category
        payload = record.to_dict()
        payload["media_type"] = file.content_type or payload.get("media_type")
        repo.put("documents", record.id, payload, task_id=task_id)
        ctx.sync_knowledge_task(task_id)
        return payload

    @router.get("/tasks/{task_id}/documents")
    def list_documents(task_id: str):
        ctx.task_or_404(task_id)
        return {"items": repo.list("documents", task_id)}

    @router.post("/tasks/{task_id}/documents/{document_id}/ocr")
    def run_document_ocr(task_id: str, document_id: str):
        ctx.task_or_404(task_id)
        raw = next((item for item in repo.list("documents", task_id) if item.get("id") == document_id), None)
        if raw is None:
            raise HTTPException(404, detail={"code": "document_not_found", "message": "资料不存在", "field": "document_id"})
        record = document_record(raw)
        vision_adapter = ctx.runtime_vision_adapter()
        if vision_adapter is None:
            raise HTTPException(503, detail={"code": "ocr_provider_not_configured", "message": "未配置多模态/OCR adapter", "field": "document_id"})
        updated = ctx.ingestion.ingest_file(
            raw["original_path"],
            document_id=record.id,
            vision_adapter=vision_adapter,
            pdf_vision_mode="all",
        )
        updated.category = record.category
        if updated.document_type not in {"image", "pdf"}:
            warning = ParseWarning("no_visual_input", "资料没有可供视觉识别的图片或 PDF 页面", severity="error")
            updated.warnings.append(warning)
            updated.model_extraction.status = "failed"
            updated.model_extraction.warnings.append(warning)
            updated.ocr_status = "failed"
        payload = updated.to_dict()
        repo.put("documents", document_id, payload, task_id=task_id)
        if updated.ocr_status == "completed":
            ctx.runtime_vision_verified = True
        return payload

    @router.delete("/tasks/{task_id}/documents/{document_id}")
    def delete_document(task_id: str, document_id: str):
        ctx.task_or_404(task_id)
        raw = next((item for item in repo.list("documents", task_id) if item.get("id") == document_id), None)
        if raw is None:
            raise HTTPException(404, detail={"code": "document_not_found", "message": "资料不存在", "field": "document_id"})
        with repo._lock:
            repo.db.execute("DELETE FROM documents WHERE id = ? AND task_id = ?", (document_id, task_id))
            repo.db.commit()
        removed = remove_task_owned_files(ctx.storage_dir, [raw])
        ctx.sync_knowledge_task(task_id)
        return {"deleted": True, "document_id": document_id, "removed_files": len(removed)}
