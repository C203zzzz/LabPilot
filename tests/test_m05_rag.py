from types import SimpleNamespace

import pytest

from backend.domain.rag import DocumentChunk, RetrievalQuery
from backend.domain.search import BodyStatus, SourceType
from backend.services.rag_service import (LexicalRetriever, assemble_context,
                                          chunks_from_document, chunks_from_source)


def test_document_pages_tables_and_pending_image_are_locatable():
    doc = SimpleNamespace(id="d1", source_uri="file:///quote.pdf", extracted_text="", pages=[SimpleNamespace(number=2, text="型号 X 参数 100", source_location="page:2")], tables=[SimpleNamespace(index=1, rows=[["型号", "X"]], source_location="page:2/table:1")], images=[SimpleNamespace(index=1, source_location="page:2/image:1", ocr_status="pending")])
    chunks = chunks_from_document(doc)
    assert {c.location for c in chunks} == {"page:2", "page:2/table:1", "page:2/image:1"}
    assert all(c.document_id == "d1" for c in chunks)


def test_source_snippet_never_becomes_evidence():
    source = SimpleNamespace(url="https://example.com/p", title="P", source_type=SourceType.MANUFACTURER_WEBSITE, body_text=None, body_status=BodyStatus.NOT_REQUESTED)
    assert chunks_from_source(source) == []
    source.body_text, source.body_status = "官方参数 型号 X", BodyStatus.FETCHED
    assert chunks_from_source(source)[0].source_url == source.url


def test_lexical_retrieval_is_deterministic_and_top_k():
    chunks = [DocumentChunk("b", "d", "型号 X 速度 100"), DocumentChunk("a", "d", "型号 X 温度范围")]
    got = LexicalRetriever(chunks).retrieve(RetrievalQuery("型号 X", top_k=1))
    assert len(got) == 1 and got[0].chunk.chunk_id == "a"
    assert got[0].match_reasons


def test_lexical_retrieval_matches_compound_chinese_query():
    chunks = [DocumentChunk("a", "d", "支持检测模式和温度控制")]
    got = LexicalRetriever(chunks).retrieve(RetrievalQuery("检测模式和温度控制", top_k=1))
    assert got and got[0].chunk.chunk_id == "a"


def test_empty_and_no_match_are_explicit():
    retriever = LexicalRetriever([DocumentChunk("a", "d", "速度")])
    assert assemble_context(retriever, RetrievalQuery(" ")).status == "empty_query"
    ctx = assemble_context(retriever, RetrievalQuery("温度"))
    assert ctx.status == "insufficient_evidence" and not ctx.answerable and ctx.context_text == ""


def test_invalid_top_k_and_duplicate_chunk_are_rejected():
    with pytest.raises(ValueError): RetrievalQuery("x", top_k=0)
    with pytest.raises(ValueError): LexicalRetriever([DocumentChunk("a", "d", "x"), DocumentChunk("a", "d", "y")])
