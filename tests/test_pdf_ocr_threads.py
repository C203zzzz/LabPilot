"""Offline assertions on the Docling thread configuration.

The local pass runs two ONNX model families. Their thread counts are not
interchangeable: RT-DETR (layout) multi-thread inference has raised native
access violations on Windows, while RapidOCR's onnxruntime session
was merely inheriting the same single-thread setting and paying for it on every
scanned page. These tests pin both facts without parsing a real PDF.
"""
import pytest

from backend.services.structured_pdf import (
    DEFAULT_OCR_THREADS,
    OCR_THREADS_ENV,
    _converter,
    layout_num_threads,
    ocr_threads,
)


def test_layout_threads_are_pinned_to_one():
    """The Windows RT-DETR regression must not be reopened by this knob."""
    assert layout_num_threads() == 1


def test_ocr_threads_default_is_conservative_and_positive():
    assert 1 < DEFAULT_OCR_THREADS <= 8


@pytest.mark.parametrize('value, expected', [
    ('1', 1),
    ('2', 2),
    ('4', 4),
    ('8', 8),
    ('16', 16),
    # Out of range is clamped, never propagated into an onnxruntime session.
    ('0', 1),
    ('-3', 1),
    ('999', 16),
    # A malformed value must not take the local pass down.
    ('four', DEFAULT_OCR_THREADS),
    ('', DEFAULT_OCR_THREADS),
    (' 4 ', 4),
])
def test_ocr_threads_reads_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv(OCR_THREADS_ENV, value)
    assert ocr_threads() == expected


def test_ocr_threads_absent_uses_default(monkeypatch):
    monkeypatch.delenv(OCR_THREADS_ENV, raising=False)
    assert ocr_threads() == DEFAULT_OCR_THREADS


def test_converter_separates_layout_and_ocr_threads(monkeypatch):
    """The knob must reach Docling's OCR options and leave layout at 1.

    Building the converter only constructs option objects; no model weights are
    loaded and no PDF is parsed, so this stays an offline assertion.
    """
    docling = pytest.importorskip('docling')
    from docling.datamodel.base_models import InputFormat

    monkeypatch.setenv(OCR_THREADS_ENV, '6')
    _converter.cache_clear()
    try:
        converter = _converter()
        options = converter.format_to_options[InputFormat.PDF].pipeline_options
        assert options.accelerator_options.num_threads == 1
        assert options.layout_batch_size == 1
        assert options.ocr_options.rapidocr_params == {
            'EngineConfig.onnxruntime.intra_op_num_threads': 6}
        # Resolution and OCR model choice are untouched by the thread change.
        assert options.ocr_options.scale == 3.0
        assert options.ocr_options.lang == ['ch']
        assert options.ocr_options.backend == 'onnxruntime'
    finally:
        _converter.cache_clear()
        assert docling is not None
