"""Tests for bounded lightweight attachment PDF extraction and Docling cancellation."""
from __future__ import annotations

import time
from types import SimpleNamespace

import fitz
import pytest

from backend.services import document_ingestion, structured_pdf
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


def _make_native_pdf(path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((36, 36), text)
    doc.save(path)
    doc.close()


def test_extract_attachment_pdf_text_caps_pages_and_caches_by_sha256(tmp_path):
    pdf = tmp_path / "manual.pdf"
    _make_native_pdf(
        pdf,
        [f"Page {i} specification: SW-CJ-1FD clean bench technical parameter value {100 + i}" for i in range(1, 15)],
    )
    cache_dir = tmp_path / "cache"

    first = structured_pdf.extract_attachment_pdf_text(pdf, cache_dir, max_pages=4, timeout=15.0)
    assert first["cache"] == "miss"
    assert first["total_pages"] == 14
    assert first["processed_pages"] == 4
    assert first["truncated"] is True
    assert first["truncated_reason"] == "max_pages"
    assert "Page 4 specification" in first["text"]
    assert "Page 5 specification" not in first["text"]

    second = structured_pdf.extract_attachment_pdf_text(pdf, cache_dir, max_pages=4, timeout=15.0)
    assert second["cache"] == "hit"
    assert second["text"] == first["text"]


def test_research_structured_pdf_text_skips_heavy_docling(research, tmp_path, monkeypatch):
    service, run, _, _ = research
    pdf = tmp_path / "attachment.pdf"
    _make_native_pdf(
        pdf,
        [f"Page {i} tender award notice SW-CJ-1FD unit price 3400 CNY laboratory equipment" for i in range(1, 6)],
    )
    payload = pdf.read_bytes()

    monkeypatch.setattr(
        document_ingestion.DocumentIngestionService,
        "ingest_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("research attachment must not invoke DocumentIngestionService")),
    )
    monkeypatch.setenv("LAB_RESEARCH_PDF_MAX_PAGES", "3")

    source = {
        "url": "https://buyer.gov.cn/att.pdf",
        "snapshot": {"sha256": "a" * 64, "size": len(payload)},
    }
    text, degraded = service.structured_pdf_text(run, source, payload)
    assert degraded == ""
    assert "Page 3 tender award notice" in text
    assert "Page 4 tender award notice" not in text
    assert source["text_provenance"]["method"] == "light_pdf_parser"
    assert source["text_provenance"]["truncated"] is True


def test_parse_document_cancel_check_terminates_active_subprocess(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    killed = []

    class FakeProc:
        pid = 54321

        def __init__(self):
            self._killed = False

        def poll(self):
            return -9 if self._killed else None

        def kill(self):
            self._killed = True
            killed.append("kill")

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(structured_pdf.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(structured_pdf, "_kill_process_tree", lambda proc: (killed.append(proc.pid), proc.kill()))

    checks = [0]

    def cancel_after_one():
        checks[0] += 1
        return checks[0] >= 2

    with pytest.raises(RuntimeError, match="已取消"):
        structured_pdf.parse_document(
            pdf,
            tmp_path / "cache",
            timeout=10,
            cancel_check=cancel_after_one,
            owner_id="run-cancel-1",
        )
    assert killed == [54321, "kill"]


def test_terminate_active_subprocesses_targets_owner_id(monkeypatch):
    terminated = []

    class RunningProc:
        pid = 11111

        def poll(self):
            return None

    proc = RunningProc()
    key = structured_pdf._register_active_proc("run-owner-x", proc)
    monkeypatch.setattr(structured_pdf, "_kill_process_tree", lambda p: terminated.append(p.pid))
    try:
        assert structured_pdf.terminate_active_subprocesses("other-owner") == 0
        assert structured_pdf.terminate_active_subprocesses("run-owner-x") == 1
        assert terminated == [11111]
    finally:
        structured_pdf._unregister_active_proc(key, proc)
