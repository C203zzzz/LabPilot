"""M19 persisted task-level RAG, backed only by locatable source text."""
from __future__ import annotations

from backend.domain.rag import DocumentChunk, EvidenceSource, EvidenceStatus, RetrievalQuery
from backend.services.rag_service import LexicalRetriever


def rebuild(repo, task_id: str) -> dict:
    chunks: list[DocumentChunk] = []
    # Uploaded document pages/tables/extracted text are source-derived. Model
    # extraction fields are intentionally never read here.
    for doc in repo.list("documents", task_id):
        for page in doc.get("pages", []):
            text = page.get("text", "")
            if text.strip():
                chunks.append(DocumentChunk(f"{doc['id']}:page:{page.get('number')}", doc["id"], text,
                    EvidenceSource.USER_DOCUMENT, page=page.get("number"), location=page.get("source_location") or f"page:{page.get('number')}", source_url=doc.get("source_uri")))
        for table in doc.get("tables", []):
            text = "\n".join("\t".join(str(v) for v in row) for row in table.get("rows", []))
            if text.strip():
                chunks.append(DocumentChunk(f"{doc['id']}:table:{table.get('index')}", doc["id"], text,
                    EvidenceSource.USER_DOCUMENT, location=table.get("source_location") or f"table:{table.get('index')}", source_url=doc.get("source_uri")))
        if not doc.get("pages") and not doc.get("tables") and doc.get("extracted_text", "").strip():
            chunks.append(DocumentChunk(f"{doc['id']}:text:1", doc["id"], doc["extracted_text"], EvidenceSource.USER_DOCUMENT,
                location="text:1", source_url=doc.get("source_uri")))
    # M04 stores both snippets and body_text; only fetched body_text is evidence.
    # 落盘层的号码掩码：RAG 表存的是 ``search_runs`` 正文的
    # **另一份副本**，所以落库前也要过同一道掩码。这里是"重建 RAG"的入口，因此修复
    # 之前写进 ``search_runs`` 的旧正文在这一步也会被掩掉，不会再抄一份脏的进 RAG 表。
    # 上面那些用户自己上传的文档不掩：那不是公开网页正文，不在掩码范围里。
    for run in repo.list("search_runs", task_id):
        for source in run.get("sources", []):
            if source.get("body_status") != "fetched" or not (source.get("body_text") or "").strip():
                continue
            source_type = source.get("source_type", "unknown")
            kind = EvidenceSource.OFFICIAL_SOURCE if source_type in {"manufacturer_website", "manufacturer_download"} else EvidenceSource.DEALER if source_type == "dealer" else EvidenceSource.UNKNOWN
            url = source.get("url", "")
            if url:
                import hashlib
                from backend.services.education_platform_sources import mask_landed_text
                did = "url-" + hashlib.sha1(url.encode()).hexdigest()[:12]
                chunks.append(DocumentChunk(f"{did}:body", did, mask_landed_text(source["body_text"]), kind,
                    location=source.get("source_location") or "body", source_url=url,
                    authority_score=source.get("authority_score"), metadata={"title": source.get("title", "")}))
    # Stable de-duplication and persisted JSON make re-opened tasks recoverable.
    unique = {c.chunk_id: c for c in chunks}
    rows = []
    for c in sorted(unique.values(), key=lambda x: x.chunk_id):
        rows.append({"chunk_id": c.chunk_id, "document_id": c.document_id, "text": c.text,
                     "source_type": c.source_type.value, "page": c.page, "location": c.location,
                     "source_url": c.source_url, "authority_score": c.authority_score,
                     "evidence_status": c.evidence_status.value, "metadata": c.metadata})
    repo.replace_rag_chunks(task_id, rows)
    return {"task_id": task_id, "chunk_count": len(rows), "chunks": rows}


def query(repo, task_id: str, request: RetrievalQuery) -> dict:
    rows = repo.list_rag_chunks(task_id)
    chunks = [DocumentChunk(r["chunk_id"], r["document_id"], r.get("text", ""), EvidenceSource(r.get("source_type", "unknown")),
                            page=r.get("page"), location=r.get("location"), source_url=r.get("source_url"),
                            authority_score=r.get("authority_score"), evidence_status=EvidenceStatus(r.get("evidence_status", "available")),
                            metadata=r.get("metadata", {})) for r in rows]
    context = LexicalRetriever(chunks).retrieve(request)
    return {"task_id": task_id, "query": request.text, "items": [{"chunk": {
        "chunk_id": x.chunk.chunk_id, "document_id": x.chunk.document_id, "text": x.chunk.text,
        "source_type": x.chunk.source_type.value, "page": x.chunk.page, "location": x.chunk.location,
        "source_url": x.chunk.source_url, "authority_score": x.chunk.authority_score, "metadata": x.chunk.metadata},
        "score": x.score, "match_reasons": list(x.match_reasons)} for x in context],
        "answerable": bool(context), "status": "ok" if context else "insufficient_evidence"}


__all__ = ["rebuild", "query"]
