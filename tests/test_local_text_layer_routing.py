"""The local text layer must survive a Docling model failure.

Why these exist
---------------
The accurate local pass (``structured_pdf.parse_document``) loads Docling's
layout and TableFormer models. On a memory-constrained Windows host that load
can fail outright (observed: ``OSError: 页面文件太小 (os error 1455)``). Because
that pass used to be all-or-nothing, a single model-load failure sent *every*
page of a scan to the paid vision model: the 17-page pure-scan acceptance
sample took ~15 minutes of serial remote calls, and a 1000-page manual would
never finish. These tests pin the replacement behaviour:

* a failed accurate pass falls back to the lightweight PyMuPDF/RapidOCR layer,
  which still recovers a text layer and routes pages to vision with a draft;
* the number of pages rendered remotely per round is bounded, and the deferred
  pages stay auditable instead of silently disappearing;
* a missing local OCR engine is reported, never treated as "page has no text".
"""
from pathlib import Path

import pytest

from backend.services.document_ingestion import DocumentIngestionService
from backend.services.structured_pdf import _native_ocr_engine
from backend.services.vision import VisionResult


@pytest.fixture(autouse=True)
def _uncached_native_ocr_engine():
    """``_native_ocr_engine`` is a process-wide ``lru_cache(maxsize=1)``.

    That is deliberate product behaviour (the engine owns onnxruntime sessions
    and is built once per process), but it makes the two tests at the bottom of
    this file order-dependent: they replace ``rapidocr.RapidOCR`` and then call
    ``native_text_regions``, which only sees the patch while the cache is still
    empty. Any earlier test that built a real engine left those two reading a
    blank page through it -- green when this file ran alone, red in a full run.

    Cleared on both sides. Before: this file must not rely on "nothing has built
    an engine yet". After: the fake engines installed by the tests here must
    never be handed to a later test.
    """
    _native_ocr_engine.cache_clear()
    yield
    _native_ocr_engine.cache_clear()


class _RecordingVision:
    provider = "recording-vision"

    def __init__(self):
        self.calls: list[str] = []

    def analyze(self, image_bytes, *, media_type, source_location):
        self.calls.append(source_location)
        return VisionResult(text=f"vision:{source_location}", provider=self.provider)


def _scan_like_pdf(path: Path, pages: int) -> None:
    """A PDF whose pages carry a full-page image and no native text."""
    import io

    import fitz
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 560), "white").save(buffer, format="PNG")
    pdf = fitz.open()
    for _ in range(pages):
        page = pdf.new_page(width=400, height=560)
        page.insert_image(fitz.Rect(0, 0, 400, 560), stream=buffer.getvalue())
    path.write_bytes(pdf.tobytes())
    pdf.close()


def _regions_for(pages: dict[int, str], parser: str = "rapidocr"):
    regions = []
    for number, text in pages.items():
        regions.append({
            'id': f'native-light:{number}:0', 'page': number,
            'bbox': {'l': 0.0, 't': 0.0, 'r': 400.0, 'b': 560.0, 'coord_origin': 'TOPLEFT'},
            'label': 'text', 'level': 1, 'text': text, 'cells': [],
            'parser': parser, 'location': f'page:{number}/native-light:0',
        })
    return {'parser': 'native_light', 'regions': regions, 'engine_error': None}


SPEC_TEXT = ("型号：H1850R 台式高速冷冻离心机 最高转速 18500r/min 最大相对离心力 23797×g "
             "最大容量 4×100mL 温度范围 -20℃～+40℃ 净重 75kg")


def _break_accurate_pass(monkeypatch):
    monkeypatch.setattr('backend.services.structured_pdf.parse_document',
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("页面文件太小，无法完成操作。")))


def test_failed_structured_pass_falls_back_to_local_text_layer(tmp_path: Path, monkeypatch):
    source = tmp_path / "scan.pdf"
    _scan_like_pdf(source, 2)
    _break_accurate_pass(monkeypatch)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: _regions_for({1: SPEC_TEXT, 2: SPEC_TEXT}))

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=_RecordingVision()).ingest_file(source)

    assert any(w.code == 'structured_parser_failed' for w in record.warnings)
    # The page keeps a locally read draft next to the paid transcription, so it
    # stays citable even before/without the remote call.
    assert record.pages[0].quality['local_text_draft'].startswith('型号：H1850R')
    assert any(item.get('parser') == 'rapidocr' for item in record.pages[0].provenance)
    assert 'H1850R' in record.extracted_text


def test_lightweight_layer_reads_every_page_when_the_accurate_pass_fails(tmp_path: Path, monkeypatch):
    source = tmp_path / "scan.pdf"
    _scan_like_pdf(source, 1)
    called = {}

    def fail(*args, **kwargs):
        raise RuntimeError('docling model load failed')

    def lightweight(path, pages=None):
        called['pages'] = pages
        return _regions_for({1: SPEC_TEXT})

    monkeypatch.setattr('backend.services.structured_pdf.parse_document', fail)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions', lightweight)

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=_RecordingVision()).ingest_file(source)

    # ``None`` means "read every page", so the text layer is complete even when
    # only some pages are later sent to the paid model.
    assert called['pages'] is None
    assert 'H1850R' in record.pages[0].text
    assert any(item.get('parser') == 'rapidocr' for item in record.pages[0].provenance)


def test_vision_page_cap_defers_pages_and_keeps_them_auditable(tmp_path: Path, monkeypatch):
    source = tmp_path / "manual.pdf"
    _scan_like_pdf(source, 5)
    monkeypatch.setenv('LAB_VISION_MAX_PAGES', '2')
    _break_accurate_pass(monkeypatch)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: _regions_for({number: SPEC_TEXT for number in range(1, 6)}))
    adapter = _RecordingVision()

    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert len(adapter.calls) == 2, adapter.calls
    deferred = [page for page in record.pages if page.route_reason == 'deferred_by_page_cap']
    assert len(deferred) == 3
    # A deferred page keeps the locally read text and stops needing a paid
    # render; nothing is left without text and nothing claims to be unpaid.
    assert all(page.route == 'native_text' for page in deferred)
    assert all(page.text.strip() for page in deferred)
    assert any(w.code == 'vision_page_cap_applied' for w in record.warnings)
    # Every deferred page was read locally, so the message must say so and must
    # not imply that all five pages needed paid remote work.
    warning = next(w for w in record.warnings if w.code == 'vision_page_cap_applied')
    assert '5 页原生文字不足采信' in warning.message
    assert '付费识别前 2 页' in warning.message
    assert '其余 3 页不再付费：3 页用本地文字' in warning.message
    assert '0 页无本地文字' in warning.message
    assert '需要远程识别' not in warning.message
    assert record.ocr_status == 'completed'


def test_deferred_page_names_the_pass_that_actually_read_it(tmp_path: Path, monkeypatch):
    """A cap-deferred page must not be labelled 'rapidocr' when Docling read it.

    Regression: the deferred branch hardcoded ``local_recovery_parser='rapidocr'``.
    On a real upload where the accurate pass succeeded, seven pages carried
    ``structured_pdf``/``docling`` provenance and were still labelled
    ``rapidocr``, so the parameter table's per-page provenance was wrong.
    """
    source = tmp_path / "manual.pdf"
    _scan_like_pdf(source, 5)
    monkeypatch.setenv('LAB_VISION_MAX_PAGES', '2')
    # The accurate pass SUCCEEDS here and supplies the text for every page.
    monkeypatch.setattr(
        'backend.services.structured_pdf.parse_document',
        lambda path, cache, pages=None: {
            'parser': 'docling',
            'regions': _regions_for({number: SPEC_TEXT for number in range(1, 6)},
                                    parser='docling')['regions'],
        })
    monkeypatch.setattr('backend.services.structured_pdf.complete_native_regions',
                        lambda path, regions, pages: regions)
    adapter = _RecordingVision()

    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    deferred = [page for page in record.pages if page.route_reason == 'deferred_by_page_cap']
    assert len(deferred) == 3, [p.number for p in deferred]
    assert all(page.quality.get('local_recovery_parser') == 'docling' for page in deferred), \
        [(p.number, p.quality.get('local_recovery_parser')) for p in deferred]


def test_explicit_page_retry_is_not_capped(tmp_path: Path, monkeypatch):
    source = tmp_path / "manual.pdf"
    _scan_like_pdf(source, 4)
    monkeypatch.setenv('LAB_VISION_MAX_PAGES', '1')
    _break_accurate_pass(monkeypatch)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: _regions_for({number: SPEC_TEXT for number in range(1, 5)}))
    adapter = _RecordingVision()
    service = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter)
    record = service.ingest_file(source)
    adapter.calls.clear()

    service.analyze_pdf_pages(record, {2, 3}, adapter)

    assert adapter.calls == ['page:2/rendered', 'page:3/rendered']


def test_missing_local_ocr_engine_is_reported_not_hidden(tmp_path: Path, monkeypatch):
    source = tmp_path / "scan.pdf"
    _scan_like_pdf(source, 1)
    _break_accurate_pass(monkeypatch)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: {'parser': 'native_light', 'regions': [],
                                                  'engine_error': 'ImportError: rapidocr missing'})

    record = DocumentIngestionService(
        tmp_path / "storage", vision_adapter=_RecordingVision()).ingest_file(source)

    codes = [warning.code for warning in record.warnings]
    assert 'local_ocr_unavailable' in codes
    assert 'structured_parser_failed' in codes


def test_vision_page_relevance_prefers_specification_pages(tmp_path: Path):
    from backend.schemas.documents import DocumentPage

    service = DocumentIngestionService(tmp_path / "storage")
    spec = DocumentPage(1, SPEC_TEXT, 'page:1', 'doc')
    cover = DocumentPage(2, '前言 本手册介绍产品使用与安全注意事项，请仔细阅读', 'page:2', 'doc')
    blank = DocumentPage(3, '', 'page:3', 'doc')

    assert service._vision_page_relevance(spec) > service._vision_page_relevance(cover)
    assert service._vision_page_relevance(cover) > service._vision_page_relevance(blank)


def test_cap_disabled_renders_every_required_page(tmp_path: Path, monkeypatch):
    source = tmp_path / "manual.pdf"
    _scan_like_pdf(source, 3)
    monkeypatch.setenv('LAB_VISION_MAX_PAGES', '0')
    _break_accurate_pass(monkeypatch)
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: _regions_for({number: SPEC_TEXT for number in range(1, 4)}))
    adapter = _RecordingVision()

    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert len(adapter.calls) == 3
    assert not any(w.code == 'vision_page_cap_applied' for w in record.warnings)


def test_deferred_page_without_local_text_keeps_document_partial(tmp_path: Path, monkeypatch):
    """Unread pages must stay visibly unpaid instead of being reported as done."""
    source = tmp_path / "manual.pdf"
    _scan_like_pdf(source, 3)
    monkeypatch.setenv('LAB_VISION_MAX_PAGES', '1')
    _break_accurate_pass(monkeypatch)
    # Only page 1 could be read locally; pages 2-3 stay without any text.
    monkeypatch.setattr('backend.services.structured_pdf.native_text_regions',
                        lambda path, pages=None: _regions_for({1: SPEC_TEXT}))
    adapter = _RecordingVision()

    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert adapter.calls == ['page:1/rendered']
    assert record.ocr_status == 'partial'
    textless = [page for page in record.pages if page.number in {2, 3}]
    assert all(page.route == 'vision' for page in textless)
    assert all(page.route_reason == 'deferred_by_page_cap' for page in textless)
    warning = next(w for w in record.warnings if w.code == 'vision_page_cap_applied')
    # The first number is the pre-cap visual queue, not "pages that need paid
    # work": pages 2-3 said empty_native_text while page 1 was read locally and
    # paid for. Here both deferred pages are textless, so the local clause is
    # dropped rather than printing "0 页用本地文字".
    assert '3 页原生文字不足采信' in warning.message
    assert '付费识别前 1 页' in warning.message
    assert '其余 2 页不再付费' in warning.message
    assert '0 页用本地文字' not in warning.message
    assert '2 页无本地文字' in warning.message
    assert '需要远程识别' not in warning.message


def test_native_text_regions_reads_local_text_without_table_models(tmp_path: Path):
    """The lightweight pass must work on a text PDF with no model weights."""
    import fitz

    from backend.services.structured_pdf import native_text_regions

    source = tmp_path / "native.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=500, height=700)
    page.insert_textbox((40, 40, 460, 660), SPEC_TEXT, fontsize=11)
    source.write_bytes(pdf.tobytes())
    pdf.close()

    parsed = native_text_regions(source, None)

    assert parsed['regions'], parsed
    assert 'H1850R' in parsed['regions'][0]['text']
    assert parsed['regions'][0]['parser'] == 'pymupdf_native'
    assert parsed['engine_error'] is None


class _NumpyLikeArray:
    """A stand-in for the ndarray RapidOCR returns in ``boxes``.

    The real object is ``numpy.ndarray``, whose ``bool()`` raises for more than
    one element. Reproducing that contract here keeps the regression test
    dependency-free while still failing if the code goes back to ``or []``.
    """

    def __init__(self, items):
        self._items = list(items)

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __bool__(self):
        if len(self._items) > 1:
            raise ValueError(
                "The truth value of an array with more than one element is ambiguous. "
                "Use a.any() or a.all()")
        return bool(self._items)


class _OcrOutput:
    """The attribute container RapidOCR returns (``txts`` / ``boxes`` / ``scores``)."""

    def __init__(self, rows):
        rows = list(rows)
        self.txts = tuple(row[0] for row in rows)
        self.boxes = _NumpyLikeArray([row[1] for row in rows])
        self.scores = tuple(row[2] for row in rows)


class _FakeRapidOCR:
    """Return the exact field shapes the real engine uses: tuple / ndarray / tuple."""

    def __init__(self, rows):
        self._rows = rows

    def __call__(self, image):
        return _OcrOutput(self._rows)


def _box(top_left, bottom_right):
    return [[top_left[0], top_left[1]], [bottom_right[0], top_left[1]],
            [bottom_right[0], bottom_right[1]], [top_left[0], bottom_right[1]]]


def test_rapidocr_array_fields_do_not_raise_on_truthiness(tmp_path: Path, monkeypatch):
    """The OCR branch must survive numpy-shaped result fields.

    Regression: ``list(getattr(result, 'boxes', None) or [])`` raises
    ``ValueError: The truth value of an array with more than one element is
    ambiguous`` because ``boxes`` is a numpy array. The exception escaped
    ``native_text_regions`` and was swallowed by ``_parse_pdf`` as
    ``local_text_layer_failed``, so the local layer never produced text on a
    real scan and every page silently stayed on paid vision.
    """
    import io

    import fitz
    from PIL import Image

    from backend.services.structured_pdf import native_text_regions

    buffer = io.BytesIO()
    Image.new("RGB", (400, 560), "white").save(buffer, format="PNG")
    source = tmp_path / "scan.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=400, height=560)
    page.insert_image(fitz.Rect(0, 0, 400, 560), stream=buffer.getvalue())
    source.write_bytes(pdf.tobytes())
    pdf.close()

    engine = _FakeRapidOCR([
        ("型号 H1850R", _box((10, 10), (120, 30)), 0.99),
        ("最高转速 18500r/min", _box((10, 40), (200, 60)), 0.98),
        ("最大相对离心力 23797×g", _box((10, 70), (220, 90)), 0.97),
    ])
    monkeypatch.setattr("rapidocr.RapidOCR", lambda *args, **kwargs: engine)

    parsed = native_text_regions(source, None)

    assert parsed['engine_error'] is None, parsed
    assert len(parsed['regions']) == 3, parsed['regions']
    assert parsed['regions'][0]['parser'] == 'rapidocr'
    assert parsed['regions'][0]['text'] == '型号 H1850R'
    assert parsed['regions'][0]['confidence'] == 0.99
    # Geometry must survive too: a bbox dropped to None would break citations.
    assert parsed['regions'][0]['bbox'] == {'l': 10.0, 't': 10.0, 'r': 120.0, 'b': 30.0,
                                           'coord_origin': 'TOPLEFT'}


def test_local_layer_recovers_text_from_a_pure_scan(tmp_path: Path, monkeypatch):
    """A scan with no native text must still yield local text end to end.

    This is the assertion the acceptance round was missing: the previous suite
    only exercised ``native_text_regions`` on a PDF that had a native text
    layer, and monkeypatched the function everywhere else, so the OCR branch was
    never executed by any test.
    """
    import io

    import fitz
    from PIL import Image

    from backend.services.document_ingestion import DocumentIngestionService
    from backend.services.vision import VisionResult

    buffer = io.BytesIO()
    Image.new("RGB", (400, 560), "white").save(buffer, format="PNG")
    source = tmp_path / "scan.pdf"
    pdf = fitz.open()
    for _ in range(2):
        page = pdf.new_page(width=400, height=560)
        page.insert_image(fitz.Rect(0, 0, 400, 560), stream=buffer.getvalue())
    source.write_bytes(pdf.tobytes())
    pdf.close()

    engine = _FakeRapidOCR([(SPEC_TEXT, _box((5, 5), (380, 40)), 0.95),
                            ("湘仪离心机", _box((5, 50), (200, 70)), 0.93)])
    monkeypatch.setattr("rapidocr.RapidOCR", lambda *args, **kwargs: engine)
    _break_accurate_pass(monkeypatch)
    monkeypatch.setenv("LAB_VISION_MAX_PAGES", "1")

    class _Vision:
        provider = "recording"

        def __init__(self):
            self.calls = []

        def analyze(self, image_bytes, *, media_type, source_location):
            self.calls.append(source_location)
            return VisionResult(text=f"vision:{source_location}", provider=self.provider)

    vision = _Vision()
    service = DocumentIngestionService(storage_dir=tmp_path / "storage")

    record = service.ingest_file(source, vision_adapter=vision)

    # The local layer ran: every page carries local text and its parser.
    assert record.pages, record
    for page in record.pages:
        assert page.text.strip(), f"page {page.number} has no local text: {page.text!r}"
    assert any((page.quality or {}).get('local_recovery_parser') == 'rapidocr'
               for page in record.pages)
    assert not any(w.code == 'local_text_layer_failed' for w in record.warnings), \
        [w.message for w in record.warnings]
