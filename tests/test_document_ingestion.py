import csv
from pathlib import Path

import pytest

from backend.services.document_ingestion import DocumentIngestionService
from backend.services.multimodal_ocr import MockMultimodalOCRAdapter, MultimodalOCRService
from backend.services.vision import VisionResult


class _PageVisionStub:
    provider = "page-vision-stub"

    def __init__(self):
        self.calls: list[str] = []

    def analyze(self, image_bytes, *, media_type, source_location):
        self.calls.append(source_location)
        return VisionResult(text=f"vision:{source_location}", provider=self.provider)


class _FirstPageFailsVision(_PageVisionStub):
    def analyze(self, image_bytes, *, media_type, source_location):
        self.calls.append(source_location)
        if source_location == "page:1/rendered":
            raise RuntimeError("first page unavailable")
        return VisionResult(text=f"vision:{source_location}", provider=self.provider)


def _text_pdf(path: Path, page_texts: list[str]) -> None:
    import fitz

    pdf = fitz.open()
    for text in page_texts:
        page = pdf.new_page(width=500, height=700)
        page.insert_textbox((40, 40, 460, 660), text, fontsize=11)
    path.write_bytes(pdf.tobytes())
    pdf.close()


def test_csv_preserves_hash_table_and_source(tmp_path: Path):
    source = tmp_path / "quote.csv"
    source.write_text("型号,价格\nA-1,1200\n", encoding="utf-8")
    service = DocumentIngestionService(tmp_path / "storage")
    result = service.ingest_file(source)

    assert result.document_type == "csv"
    assert len(result.sha256) == 64
    assert result.tables[0].rows[1] == ["A-1", "1200"]
    assert result.pages[0].source_location == "sheet:1"
    assert Path(result.original_path).exists()
    assert result.model_extraction.status == "not_run"


def test_image_is_retained_and_ocr_is_explicit(tmp_path: Path):
    source = tmp_path / "spec.png"
    source.write_bytes(b"not-a-real-png-but-source-bytes")
    result = DocumentIngestionService(tmp_path / "storage").ingest_file(source)

    assert result.document_type == "image"
    assert result.ocr_status == "failed"
    assert result.images[0].sha256 == result.sha256
    assert any(w.code == "image_decode_failed" for w in result.warnings)


def test_duplicate_bytes_are_reported_without_overwriting_original(tmp_path: Path):
    first = tmp_path / "first.txt"
    second = tmp_path / "renamed.txt"
    first.write_text("same source", encoding="utf-8")
    second.write_bytes(first.read_bytes())
    service = DocumentIngestionService(tmp_path / "storage")
    one = service.ingest_file(first)
    two = service.ingest_file(second)

    assert one.sha256 == two.sha256
    assert one.original_path == two.original_path
    assert any(w.code == "duplicate_source" for w in two.warnings)


def test_mock_multimodal_result_is_persisted_as_pending_review(tmp_path: Path):
    from PIL import Image

    source = tmp_path / "ls-50hd.jpg"
    Image.new("RGB", (100, 100), "white").save(source, format="JPEG")
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)
    updated = MultimodalOCRService(tmp_path / "storage", MockMultimodalOCRAdapter()).extract(record)

    assert updated.ocr_status == "completed"
    assert updated.images[0].ocr_status == "completed"
    assert updated.model_extraction.status == "completed"
    assert updated.model_extraction.review_status == "pending_review"
    assert updated.model_extraction.fields["model"]["value"] == "LS-50HD"
    assert updated.model_extraction.fields["model"]["status"] == "pending_review"
    assert updated.model_extraction.fields["pressure"]["unit"] == "MPa"
    assert updated.model_extraction.source_refs[0]["location"] == "image:1"


def test_fixed_ls50hd_jpg_is_a_real_visual_regression_input(tmp_path: Path):
    source = Path(r"C:\fixtures\ls50hd-visual-regression.jpg")
    if not source.is_file():
        # The checked-out acceptance storage keeps the same immutable fixture
        # when the original external desktop path is unavailable.
        source = Path("storage/originals/3176cf3cf9657ac761219093e5ace94223d56351d57118aa0f9e5eb9057ba8f2.jpg")
    if not source.is_file():
        import pytest
        pytest.skip("固定 JPG 仅存在于指定验收环境")
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)
    updated = MultimodalOCRService(tmp_path / "storage", MockMultimodalOCRAdapter()).extract(record)

    assert updated.sha256 == "3176cf3cf9657ac761219093e5ace94223d56351d57118aa0f9e5eb9057ba8f2"
    assert updated.model_extraction.fields["model"]["value"] == "LS-50HD"
    assert updated.model_extraction.review_status == "pending_review"
    assert all(field["status"] == "pending_review" for field in updated.model_extraction.fields.values())
    assert {ref["location"] for ref in updated.model_extraction.source_refs} == {"image:1"}


def test_scanned_pdf_pages_render_and_reuse_same_adapter(tmp_path: Path):
    import io
    from PIL import Image
    source = tmp_path / "scan.pdf"
    import fitz
    image = Image.new("RGB", (220, 120), "white")
    buf = io.BytesIO(); image.save(buf, format="PNG")
    pdf = fitz.open()
    for _ in range(2):
        page = pdf.new_page(width=220, height=120)
        page.insert_image(page.rect, stream=buf.getvalue())
    pdf.save(str(source)); pdf.close()
    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)
    adapter = MockMultimodalOCRAdapter()
    updated = MultimodalOCRService(tmp_path / "storage", adapter).extract(record)

    assert updated.ocr_status == "completed"
    assert [item.location for item in adapter.calls] == ["page:1", "page:2"]
    assert all(Path(item.path).exists() for item in adapter.calls)
    assert all(item["location"].startswith("page:") for item in updated.model_extraction.source_refs)


def test_reliable_native_pdf_page_skips_visual_model_and_records_profile(tmp_path: Path):
    source = tmp_path / "native.pdf"
    _text_pdf(source, [
        "A neutral technical document with enough selectable text to retain "
        "the native PDF layer without visual processing. " * 3
    ])
    adapter = _PageVisionStub()

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=adapter,
    ).ingest_file(source)

    assert adapter.calls == []
    assert record.ocr_status == "not_requested"
    assert record.pages[0].route == "native_text"
    assert record.pages[0].route_reason == "native_text_reliable"
    assert record.pages[0].quality["label"] == "reliable"
    assert record.pages[0].quality["char_count"] >= 32
    assert record.pages[0].provenance == [{
        "kind": "native_pdf_text",
        "location": "page:1",
        "status": "available",
        "parser": "pypdf",
        "char_count": record.pages[0].quality["char_count"],
    }]


def test_page_quality_gate_routes_empty_short_and_corrupt_text_to_vision():
    cases = [
        ("", "empty_native_text"),
        ("short", "very_low_native_text"),
        (("\ufffd" * 10) + ("readable" * 8), "corrupt_native_text"),
        (("\ue000" * 10) + ("readable" * 8), "corrupt_native_text"),
    ]

    for text, expected_reason in cases:
        quality, route, reason = DocumentIngestionService._assess_pdf_page(text)
        assert route == "vision"
        assert reason == expected_reason
        assert quality["label"] in {"empty", "poor"}


def test_layout_risk_gate_is_explainable_and_category_independent():
    text = "Neutral alpha numeric specification value 1234. " * 12
    quality, route, reason = DocumentIngestionService._assess_pdf_page(
        text,
        {
            "image_count": 1,
            "image_coverage": 0.62,
            "text_block_count": 10,
            "text_coverage": 0.12,
            "drawing_count": 0,
            "table_count": 0,
        },
    )

    assert route == "vision"
    assert reason == "complex_layout_risk"
    assert quality["layout_risk_score"] >= 2
    assert quality["signals"] == ["image_dominant_page"]


def test_structured_table_suppresses_vector_grid_false_positive():
    text = "\n".join(f"row {index} value" for index in range(12))
    quality, route, reason = DocumentIngestionService._assess_pdf_page(
        text,
        {
            "text_block_count": 12,
            "text_coverage": 0.20,
            "drawing_count": 40,
            "table_count": 1,
        },
    )

    assert route == "native_text"
    assert reason == "native_text_reliable"
    assert "dense_vector_layout" not in quality["signals"]


def test_force_all_mode_keeps_full_page_visual_fallback(tmp_path: Path):
    source = tmp_path / "forced.pdf"
    _text_pdf(source, ["This clean native text is intentionally forced through vision. " * 4])
    adapter = _PageVisionStub()

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=adapter, pdf_vision_mode="all",
    ).ingest_file(source)

    assert adapter.calls == ["page:1/rendered"]
    assert record.pages[0].route == "vision"
    assert record.pages[0].route_reason == "forced_all_pages"
    assert record.pages[0].quality["label"] == "reliable"
    assert record.pages[0].provenance[-1]["kind"] == "visual_page_analysis"
    assert record.pages[0].provenance[-1]["status"] == "completed"


def test_one_failed_visual_page_keeps_document_failed_after_later_success(tmp_path: Path):
    source = tmp_path / "partial-failure.pdf"
    _text_pdf(source, ["short", "short"])
    adapter = _FirstPageFailsVision()

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=adapter,
    ).ingest_file(source)

    assert adapter.calls == ["page:1/rendered", "page:2/rendered"]
    assert record.ocr_status == "failed"
    assert record.model_extraction.status == "failed"
    assert [page.provenance[-1]["status"] for page in record.pages] == ["failed", "completed"]


def test_image_dominant_page_with_sufficient_text_uses_adaptive_vision(tmp_path: Path, monkeypatch):
    # Exercise the fallback explicitly; installed Docling may already resolve
    # this layout without a paid vision request.
    def unavailable(*args, **kwargs):
        raise ImportError('optional structured parser unavailable')
    monkeypatch.setattr('backend.services.structured_pdf.parse_document', unavailable)
    import io

    import fitz
    from PIL import Image

    source = tmp_path / "mixed-layout.pdf"
    image_buffer = io.BytesIO()
    Image.new("RGB", (600, 600), "white").save(image_buffer, format="PNG")
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_image(fitz.Rect(0, 0, 600, 600), stream=image_buffer.getvalue())
    page.insert_textbox(
        fitz.Rect(30, 610, 570, 790),
        "Generic selectable technical text with enough characters for the native layer. " * 4,
        fontsize=10,
    )
    source.write_bytes(pdf.tobytes())
    pdf.close()
    adapter = _PageVisionStub()

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=adapter,
    ).ingest_file(source)

    assert adapter.calls == ["page:1/rendered"]
    assert record.pages[0].quality["char_count"] >= 32
    assert record.pages[0].route_reason == "complex_layout_risk"
    assert "image_dominant_page" in record.pages[0].quality["signals"]


def test_pdfplumber_tables_are_independent_page_located_elements(tmp_path: Path):
    pytest.importorskip("pdfplumber")
    import fitz

    source = tmp_path / "table.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=400, height=300)
    left, bottom, cell_width, cell_height = 40, 120, 150, 40
    for column in range(3):
        x = left + column * cell_width
        page.draw_line((x, bottom), (x, bottom + 2 * cell_height))
    for row in range(3):
        y = bottom + row * cell_height
        page.draw_line((left, y), (left + 2 * cell_width, y))
    page.insert_text((left + 5, bottom + 25), "Field")
    page.insert_text((left + cell_width + 5, bottom + 25), "Value")
    page.insert_text((left + 5, bottom + cell_height + 25), "Capacity")
    page.insert_text((left + cell_width + 5, bottom + cell_height + 25), "96")
    source.write_bytes(pdf.tobytes())
    pdf.close()

    record = DocumentIngestionService(tmp_path / "storage").ingest_file(source)

    assert len(record.tables) == 1
    assert record.tables[0].source_location == "page:1/table:1"
    assert record.tables[0].rows == [["Field", "Value"], ["Capacity", "96"]]
    assert record.pages[0].quality["table_count"] == 1


def test_pdf_parse_failure_warning_records_page_counts(tmp_path: Path, monkeypatch):
    """``pdf_parse_failed`` 必须写清"已解析几页、整份几页"（``docs/209`` §3.4）。

    这是 ``_parse_pdf`` 整份失败的唯一出口。页级问题各有各的出口，能冒泡到这里
    的只有两种：``PdfReader`` 构造失败（页清单根本不存在）与 ``reader.pages``
    惰性解析中断（部分页已解析）。两种都要在 warning 里留下可排查的页数事实，
    同时**不改变** ``has_current_pdf_profile`` 的既有判定——这条记录依旧需要重解析。
    """
    import pypdf

    from backend.services.document_ingestion import (
        DocumentIngestionService,
        _pdf_page_counts,
    )

    body = (
        "A neutral technical document with enough selectable text to retain "
        "the native PDF layer without visual processing. " * 3
    )
    source = tmp_path / "truncated.pdf"
    _text_pdf(source, [body] * 5)
    real_reader = pypdf.PdfReader

    class _PagesTruncatedAfter:
        def __init__(self, reader, limit: int):
            self._reader = reader
            self._limit = limit

        def __len__(self):
            return len(self._reader.pages)

        def __iter__(self):
            for index, page in enumerate(self._reader.pages):
                if index >= self._limit:
                    raise RuntimeError("page tree truncated")
                yield page

    class _TruncatedReader:
        def __init__(self, stream):
            self._reader = real_reader(stream)

        @property
        def pages(self):
            return _PagesTruncatedAfter(self._reader, 2)

    monkeypatch.setattr(pypdf, "PdfReader", _TruncatedReader)
    partial = DocumentIngestionService(tmp_path / "storage").ingest_file(source)

    failure = next(w for w in partial.warnings if w.code == "pdf_parse_failed")
    assert failure.severity == "error"
    # 已解析 2 页、整份 5 页，且明说剩下的页没进页清单——内部事实不许被说粗。
    assert "已解析 2 页" in failure.message
    assert "整份共 5 页" in failure.message
    assert "其余页面未进入页清单" in failure.message
    assert len(partial.pages) == 2
    # 连带影响：warning 的 message 变详细，判定口径不动——仍需重解析。
    assert DocumentIngestionService.has_current_pdf_profile(partial) is False

    def _cannot_open(stream):
        raise RuntimeError("cannot read page tree")

    monkeypatch.setattr(pypdf, "PdfReader", _cannot_open)
    unreadable = DocumentIngestionService(tmp_path / "storage").ingest_file(source)

    broken = next(w for w in unreadable.warnings if w.code == "pdf_parse_failed")
    # 页清单没建立起来：写明拿不到总页数，而不是编一个数或干脆不提。
    assert "已解析 0 页" in broken.message
    assert "整份总页数未能获取" in broken.message
    assert DocumentIngestionService.has_current_pdf_profile(unreadable) is False
    # 直连 helper：reader 与 layout_pdf 都拿不到时也不许抛错。
    assert _pdf_page_counts(unreadable, None, None, 0) == (
        "已解析 0 页，整份总页数未能获取（页清单未建立）"
    )


def test_unsupported_format_is_a_visible_error(tmp_path: Path):
    source = tmp_path / "data.bin"
    source.write_bytes(b"x")
    result = DocumentIngestionService(tmp_path / "storage").ingest_file(source)

    assert any(w.code == "unsupported_format" and w.severity == "error" for w in result.warnings)


def test_invalid_url_does_not_fetch_or_invent_content(tmp_path: Path):
    result = DocumentIngestionService(tmp_path / "storage").ingest_url("file:///secret.txt")

    assert result.document_type == "webpage"
    assert result.extracted_text == ""
    assert result.warnings[0].code == "invalid_url"
