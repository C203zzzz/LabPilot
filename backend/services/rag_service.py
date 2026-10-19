"""Deterministic, offline lexical retrieval for task documents and M04 sources."""
from __future__ import annotations

import re
from hashlib import sha1
from typing import Iterable

from backend.domain.rag import (DocumentChunk, EvidenceSource, EvidenceStatus,
                                RAGContext, RetrievedEvidence, RetrievalQuery)
from backend.domain.search import BodyStatus, SourceType


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for value in re.findall(r"[\w\u4e00-\u9fff]+", text or ""):
        value = value.lower()
        if not value.strip():
            continue
        tokens.add(value)
        # Chinese text has no whitespace boundaries. Keep the full phrase for
        # exact matches, then add adjacent bigrams so queries such as
        # “检测模式和温度控制” can retrieve pages containing “检测模式” and
        # “温度控制” without depending on an external tokenizer.
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            tokens.update(value[index:index + 2] for index in range(len(value) - 1))
    return tokens


def chunks_from_document(document) -> list[DocumentChunk]:
    """Convert M02 DocumentRecord pages/tables/images into stable chunks."""
    if not getattr(document, "id", None): raise ValueError("document_id is required")
    out: list[DocumentChunk] = []
    seen: set[tuple[str, str]] = set()
    for page in getattr(document, "pages", []) or []:
        text = page.text or ""
        key = (str(page.source_location or f"page:{page.number}"), text)
        if key in seen: continue
        seen.add(key)
        cid = f"{document.id}:page:{page.number}"
        out.append(DocumentChunk(cid, document.id, text, page=page.number, location=page.source_location or f"page:{page.number}", source_url=getattr(document, "source_uri", None), evidence_status=EvidenceStatus.AVAILABLE if text.strip() else EvidenceStatus.UNAVAILABLE))
    for table in getattr(document, "tables", []) or []:
        text = "\n".join("\t".join(str(v) for v in row) for row in table.rows)
        loc = table.source_location or f"table:{table.index}"
        if (loc, text) in seen: continue
        seen.add((loc, text))
        out.append(DocumentChunk(f"{document.id}:table:{table.index}", document.id, text, location=loc, source_url=getattr(document, "source_uri", None), metadata={"kind": "table"}, evidence_status=EvidenceStatus.AVAILABLE if text.strip() else EvidenceStatus.UNAVAILABLE))
    for image in getattr(document, "images", []) or []:
        loc = image.source_location or f"image:{image.index}"
        out.append(DocumentChunk(f"{document.id}:image:{image.index}", document.id, "", location=loc, source_url=getattr(document, "source_uri", None), metadata={"kind": "image", "ocr_status": str(image.ocr_status)}, evidence_status=EvidenceStatus.UNAVAILABLE))
    if not out and getattr(document, "extracted_text", ""):
        out.append(DocumentChunk(f"{document.id}:text:1", document.id, document.extracted_text, location="text:1", source_url=getattr(document, "source_uri", None)))
    return out


def chunks_from_source(source) -> list[DocumentChunk]:
    """Only fetched M04 body_text is evidence; snippets are deliberately ignored."""
    if not getattr(source, "url", None): raise ValueError("source URL is required")
    body = source.body_text or ""
    body_status = getattr(source, "body_status", BodyStatus.NOT_REQUESTED)
    if body_status not in (BodyStatus.FETCHED, BodyStatus.FETCHED.value) or not body.strip(): return []
    st = getattr(source, "source_type", SourceType.UNKNOWN)
    kind = EvidenceSource.OFFICIAL_SOURCE if st in (SourceType.MANUFACTURER_WEBSITE, SourceType.MANUFACTURER_DOWNLOAD) else EvidenceSource.DEALER if st == SourceType.DEALER else EvidenceSource.UNKNOWN
    did = "url-" + sha1(source.url.encode()).hexdigest()[:12]
    return [DocumentChunk(f"{did}:body", did, body, kind, location="body", source_url=source.url, authority_score=getattr(source, "authority_score", None), metadata={"title": source.title or ""})]


class LexicalRetriever:
    def __init__(self, chunks: Iterable[DocumentChunk] = ()):
        self.chunks = list(chunks)
        ids = [c.chunk_id for c in self.chunks]
        if len(ids) != len(set(ids)): raise ValueError("duplicate chunk_id")

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedEvidence]:
        if not query.text.strip() and not (query.parameter_terms or query.requirement_terms or query.candidate_model): return []
        terms = _tokens(" ".join((query.text, query.candidate_model or "", *query.parameter_terms, *query.requirement_terms)))
        scored = []
        for c in self.chunks:
            if c.evidence_status != EvidenceStatus.AVAILABLE or not c.text.strip(): continue
            matched = terms & _tokens(c.text)
            if not matched: continue
            score = len(matched) / max(len(terms), 1)
            reasons = tuple(f"匹配词：{x}" for x in sorted(matched))
            scored.append(RetrievedEvidence(c, round(score, 6), reasons))
        scored.sort(key=lambda x: (-x.score, x.chunk.chunk_id))
        return scored[:query.top_k]


def assemble_context(retriever: LexicalRetriever, query: RetrievalQuery) -> RAGContext:
    evidence = retriever.retrieve(query)
    has_terms = bool(query.text.strip() or query.candidate_model or query.parameter_terms or query.requirement_terms)
    return RAGContext(query, evidence, "ok" if evidence else ("empty_query" if not has_terms else "insufficient_evidence"), bool(evidence))


class RAGService:
    """Small facade kept stable for API integration; no persistence or LLM calls."""
    def __init__(self, chunks: Iterable[DocumentChunk] = ()):
        self.retriever = LexicalRetriever(chunks)

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedEvidence]:
        return self.retriever.retrieve(query)

    def context(self, query: RetrievalQuery) -> RAGContext:
        return assemble_context(self.retriever, query)


# Descriptive aliases for callers that prefer builder terminology.
build_document_chunks = chunks_from_document
build_source_chunks = chunks_from_source


__all__ = ["chunks_from_document", "chunks_from_source", "build_document_chunks", "build_source_chunks", "LexicalRetriever", "RAGService", "assemble_context"]
