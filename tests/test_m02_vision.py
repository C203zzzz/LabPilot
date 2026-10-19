from pathlib import Path
import json

import pytest

from backend.services.document_ingestion import DocumentIngestionService
from backend.services.vision import (
    OpenAICompatibleVisionAdapter,
    VisionAdapterError,
    VisionResult,
    vision_json_retries,
    vision_timeout,
)


class StubVision:
    provider = "stub-vision"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def analyze(self, image_bytes, *, media_type, source_location):
        self.calls.append((len(image_bytes), media_type, source_location))
        if self.error:
            raise self.error
        return self.result


def _png(path: Path) -> None:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (160, 80), "white")
    ImageDraw.Draw(image).text((5, 5), "LS-50HD", fill="black")
    image.save(path, format="PNG")


def test_image_vision_preserves_verbatim_character_errors_and_pending_fields(tmp_path: Path):
    source = tmp_path / "spec.png"
    _png(source)
    adapter = StubVision(VisionResult(text="LS-50HO 型号", fields={"model": "LS-50HO"}))
    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert record.ocr_status == "completed"
    image = record.images[0]
    assert image.ocr_status == "completed"
    assert image.ocr_text == "LS-50HO 型号"  # no silent OCR correction
    assert image.source_location.startswith("image:1")
    assert image.structured_fields["model"]["status"] == "pending_review"
    assert record.model_extraction.status == "completed"
    assert record.pages and record.pages[0].source_location.startswith("image:1")
    assert adapter.calls[0][1] == "image/png"


def test_image_vision_empty_result_is_failed_and_located(tmp_path: Path):
    source = tmp_path / "empty.jpg"
    _png(source)
    adapter = StubVision(VisionResult())
    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert record.images[0].ocr_status == "failed"
    assert record.ocr_status == "failed"
    assert record.images[0].ocr_text == ""
    assert any(w.code == "ocr_empty_result" and w.location.startswith("image:1") for w in record.warnings)


def test_image_vision_model_failure_is_explicit(tmp_path: Path):
    source = tmp_path / "failed.png"
    _png(source)
    adapter = StubVision(error=RuntimeError("provider unavailable"))
    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert record.images[0].ocr_status == "failed"
    assert record.ocr_status == "failed"
    assert record.model_extraction.status == "failed"
    assert any(w.code == "ocr_adapter_failed" for w in record.warnings)


def test_explicit_pdf_vision_renders_text_page_for_layout_context(tmp_path: Path):
    import fitz
    source = tmp_path / "scan.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=300, height=150)
    page.insert_text((20, 50), "LS-50HD", fontsize=20)
    source.write_bytes(pdf.tobytes())
    pdf.close()
    adapter = StubVision(VisionResult(text="LS-50HD", fields={"model": {"value": "LS-50HD"}}))
    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert len(adapter.calls) == 1
    assert any(image.source_location == "page:1/rendered" for image in record.images)


def test_scanned_pdf_empty_page_is_rendered(tmp_path: Path):
    import fitz
    from PIL import Image
    import io

    source = tmp_path / "scan-image.pdf"
    image = Image.new("RGB", (120, 80), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    pdf = fitz.open()
    page = pdf.new_page(width=120, height=80)
    page.insert_image(page.rect, stream=buf.getvalue())
    source.write_bytes(pdf.tobytes())
    pdf.close()
    adapter = StubVision(VisionResult(text="LS-50HD", fields={"model": "LS-50HD"}))
    record = DocumentIngestionService(tmp_path / "storage", vision_adapter=adapter).ingest_file(source)

    assert any(i.source_location == "page:1/rendered" and i.ocr_status == "completed" for i in record.images)
    assert any(p.source_location == "page:1" and "LS-50HD" in p.text for p in record.pages)


def test_selected_pdf_page_retry_preserves_completed_pages(tmp_path: Path):
    import fitz

    source = tmp_path / "two-pages.pdf"
    pdf = fitz.open()
    for text in ("MODEL-A", "MODEL-B"):
        page = pdf.new_page(width=300, height=150)
        page.insert_text((20, 50), text, fontsize=20)
    source.write_bytes(pdf.tobytes())
    pdf.close()

    first = StubVision(VisionResult(text="first pass", fields={"model": "A"}))
    service = DocumentIngestionService(tmp_path / "storage", vision_adapter=first)
    record = service.ingest_file(source)
    assert len(record.pages) == 2
    page_one_text = next(i.ocr_text for i in record.images if i.source_location == "page:1/rendered")

    retry = StubVision(VisionResult(text="second page retry", fields={"model": "B"}))
    service.analyze_pdf_pages(record, {2}, retry)

    assert len(retry.calls) == 1
    assert next(i.ocr_text for i in record.images if i.source_location == "page:1/rendered") == page_one_text
    assert next(i.ocr_text for i in record.images if i.source_location == "page:2/rendered") == "second page retry"


def test_vision_probe_requires_the_image_only_random_sequence(monkeypatch):
    digits = iter("234568")
    monkeypatch.setattr("backend.services.vision.secrets.choice", lambda _alphabet: next(digits))
    observed = {}

    def transport(endpoint, payload, headers, timeout):
        observed.update(endpoint=endpoint, payload=payload, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": json.dumps({"sequence": "234568"})}}]}

    adapter = OpenAICompatibleVisionAdapter(
        api_key="runtime-only", endpoint="https://example.test/chat/completions",
        model="vision-model", transport=transport,
    )
    adapter.probe()

    image_url = observed["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert observed["payload"]["messages"][0]["content"][0]["text"].find("234568") == -1
    assert observed["headers"]["Authorization"] == "Bearer runtime-only"


def test_vision_probe_rejects_a_text_only_or_incorrect_answer(monkeypatch):
    monkeypatch.setattr("backend.services.vision.secrets.choice", lambda _alphabet: "2")
    adapter = OpenAICompatibleVisionAdapter(
        api_key="runtime-only", endpoint="https://example.test/chat/completions",
        model="text-only-model",
        transport=lambda *_args: {
            "choices": [{"message": {"content": '{"sequence":"222223"}'}}],
        },
    )

    with pytest.raises(VisionAdapterError) as error:
        adapter.probe()

    assert error.value.code == "vision_probe_failed"


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def _recording_transport(replies):
    calls = []

    def transport(endpoint, payload, headers, timeout):
        calls.append(payload["messages"][0]["content"][0]["text"])
        return _reply(next(replies))

    return calls, transport


def _analyze(adapter):
    return adapter.analyze(b"image-bytes", media_type="image/png", source_location="image:1")


def _vision_adapter(transport, *, json_retries=1, timeout=45.0):
    """Build the adapter the way the runtime chain does, without any endpoint env."""
    return OpenAICompatibleVisionAdapter(
        api_key="test-vision-key",
        endpoint="https://vision.test/v1/chat/completions",
        model="vision-model",
        timeout=timeout,
        json_retries=json_retries,
        transport=transport,
    )


def test_vision_retries_one_bad_json_reply_and_then_succeeds():
    calls, transport = _recording_transport(iter([
        "这不是 JSON",
        json.dumps({"text": "LS-50HD 转速 12000 rpm", "fields": {}}),
    ]))
    # No LAB_VISION_JSON_RETRIES in env: the default must already arm one retry.
    adapter = _vision_adapter(transport, json_retries=vision_json_retries(values={}))

    result = _analyze(adapter)

    assert result.text == "LS-50HD 转速 12000 rpm"
    assert len(calls) == 2
    assert "上一次输出为空" not in calls[0]
    assert "上一次输出为空" in calls[1]


def test_vision_retries_one_empty_result_and_then_succeeds():
    calls, transport = _recording_transport(iter([
        json.dumps({"text": "", "fields": {}}),
        json.dumps({"text": "MODEL-B 主机", "fields": {"model": "MODEL-B"}}),
    ]))
    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=transport, json_retries=1,
    )

    result = _analyze(adapter)

    assert result.text == "MODEL-B 主机"
    assert result.fields == {"model": "MODEL-B"}
    assert len(calls) == 2
    assert "上一次输出为空" in calls[1]


def test_vision_exhausted_retries_keep_the_previous_error_codes():
    bad_json_calls, bad_json = _recording_transport(iter(["还是不是 JSON"] * 3))
    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=bad_json, json_retries=2,
    )
    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)
    assert error.value.code == "vision_invalid_response"
    assert len(bad_json_calls) == 3

    empty_calls, empty = _recording_transport(iter([json.dumps({"text": "", "fields": {}})] * 3))
    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=empty, json_retries=2,
    )
    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)
    assert error.value.code == "vision_empty_result"
    assert len(empty_calls) == 3


def test_vision_bad_json_without_retries_keeps_the_previous_error_code():
    calls, transport = _recording_transport(iter(["这不是 JSON"]))
    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=transport, json_retries=0,
    )

    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)

    assert error.value.code == "vision_invalid_response"
    assert len(calls) == 1


def test_vision_empty_result_without_retries_keeps_the_previous_error_code():
    calls, transport = _recording_transport(iter([json.dumps({"text": "", "fields": {}})]))
    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=transport, json_retries=0,
    )

    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)

    assert error.value.code == "vision_empty_result"
    assert len(calls) == 1


def test_vision_provider_and_network_failures_are_never_retried():
    attempts = []

    def provider_error(endpoint, payload, headers, timeout):
        attempts.append(endpoint)
        raise VisionAdapterError("vision_provider_unavailable", "视觉服务返回 HTTP 503")

    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=provider_error, json_retries=3,
    )
    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)
    assert error.value.code == "vision_provider_unavailable"
    assert len(attempts) == 1

    timeouts = []

    def timeout(*_args):
        timeouts.append(1)
        raise TimeoutError("visual endpoint is slow")

    adapter = OpenAICompatibleVisionAdapter(
        api_key="test-vision-key", endpoint="https://vision.test/chat/completions",
        model="vision-model", transport=timeout, json_retries=3,
    )
    with pytest.raises(VisionAdapterError) as error:
        _analyze(adapter)
    assert error.value.code == "vision_provider_timeout"
    assert len(timeouts) == 1


def test_vision_json_retries_env_defaults_to_one_and_is_bounded():
    # The runtime chain reads the budget through this parser instead of an
    # adapter factory, so the parser itself carries the default and the bounds.
    assert vision_json_retries(values={}) == 1

    for value, expected in (("0", 0), ("2", 2), ("3", 3)):
        assert vision_json_retries(values={"LAB_VISION_JSON_RETRIES": value}) == expected

    for invalid in ("-1", "4", "abc", "1.5", ""):
        with pytest.raises(VisionAdapterError) as error:
            vision_json_retries(values={"LAB_VISION_JSON_RETRIES": invalid})
        assert error.value.code == "vision_provider_not_configured"
        assert "重试次数" in error.value.message
        # 对外必须能看出"哪一项、写的什么、允许什么"。
        assert "LAB_VISION_JSON_RETRIES" in error.value.message
        assert error.value.field == "LAB_VISION_JSON_RETRIES"

    with pytest.raises(VisionAdapterError) as blank:
        vision_json_retries(values={"LAB_VISION_JSON_RETRIES": ""})
    # 空串也要说成"（空）"，不能留一段空白让人猜自己写了什么。
    assert "的值 （空）" in blank.value.message


def test_vision_timeout_env_defaults_to_45_and_is_bounded():
    assert vision_timeout(values={}) == 45.0

    for value, expected in (("0.5", 0.5), ("90", 90.0), ("180", 180.0)):
        assert vision_timeout(values={"LAB_VISION_TIMEOUT": value}) == expected

    for invalid in ("0", "-1", "181", "abc", ""):
        with pytest.raises(VisionAdapterError) as error:
            vision_timeout(values={"LAB_VISION_TIMEOUT": invalid})
        assert error.value.code == "vision_provider_not_configured"
        assert "超时" in error.value.message


def test_vision_env_retry_budget_reaches_the_request_loop():
    calls, transport = _recording_transport(iter([
        json.dumps({"text": "", "fields": {}}),
        json.dumps({"text": "第二次才拿到正文", "fields": {}}),
    ]))
    adapter = _vision_adapter(transport, json_retries=vision_json_retries(values={}))

    assert _analyze(adapter).text == "第二次才拿到正文"
    assert len(calls) == 2  # the unset variable really did arm one retry

    zero_calls, zero_transport = _recording_transport(iter(["这不是 JSON"]))
    disabled = _vision_adapter(zero_transport, json_retries=vision_json_retries(
        values={"LAB_VISION_JSON_RETRIES": "0"}))

    with pytest.raises(VisionAdapterError) as error:
        _analyze(disabled)

    assert error.value.code == "vision_invalid_response"
    assert len(zero_calls) == 1  # 0 keeps the pre-change single-attempt behaviour
