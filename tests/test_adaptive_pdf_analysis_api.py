from __future__ import annotations

from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from backend.infrastructure.deepseek import DeepSeekClient
from backend.main import create_app
from backend.services.document_ingestion import (
    PDF_PAGE_PROFILE_VERSION,
    DocumentIngestionService,
)
from backend.services.vision import VisionAdapterError, VisionResult


class TrackingVision:
    provider = "tracking-vision"

    def __init__(self, *, fail_locations: set[str] | None = None, fail_error=None):
        self.fail_locations = fail_locations or set()
        self.fail_error = fail_error
        self.calls: list[str] = []

    def analyze(self, image_bytes, *, media_type, source_location):
        self.calls.append(source_location)
        if source_location in self.fail_locations:
            raise (self.fail_error or RuntimeError("visual provider unavailable"))
        return VisionResult(
            text=f"visual evidence from {source_location}",
            fields={"model": {"value": "GENERIC-1"}},
            source_refs=[{"location": source_location, "quote": "GENERIC-1"}],
            provider=self.provider,
        )


def _make_pdf(path: Path, page_texts: list[str]) -> None:
    pdf = fitz.open()
    for text in page_texts:
        page = pdf.new_page(width=500, height=700)
        if text:
            page.insert_textbox((40, 40, 460, 660), text, fontsize=11)
    path.write_bytes(pdf.tobytes())
    pdf.close()


def _make_image_dominant_pdf(path: Path, text: str) -> None:
    """一页"文字达标、但整页被一张大图压住"的 PDF：profile 判它版面复杂。"""
    pdf = fitz.open()
    page = pdf.new_page(width=500, height=700)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(fitz.Rect(40, 40, 460, 600), pixmap=pix)
    page.insert_textbox((40, 615, 460, 690), text, fontsize=11)
    path.write_bytes(pdf.tobytes())
    pdf.close()


def _open_app(monkeypatch, tmp_path: Path, adapter: TrackingVision) -> TestClient:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "runtime-test-key")
    monkeypatch.setattr(
        DeepSeekClient,
        "analyze",
        lambda self, **kwargs: {"candidates": []},
    )
    return TestClient(create_app(
        database_path=tmp_path / "app.sqlite3",
        storage_dir=tmp_path / "api-storage",
        multimodal_adapter=adapter,
    ))


def _store_document(client: TestClient, payload: dict) -> str:
    task_id = client.post(
        "/api/v1/tasks", json={"scenario": "generic procurement"},
    ).json()["task"]["id"]
    client.app.state.repository.put(
        "documents", payload["id"], payload, task_id=task_id,
    )
    return task_id


def test_analysis_skips_vision_for_fully_native_pdf(monkeypatch, tmp_path: Path):
    source = tmp_path / "native.pdf"
    _make_pdf(source, ["Clean selectable source text with sufficient length. " * 8])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    adapter = TrackingVision()

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 200
    assert adapter.calls == []


def test_analysis_calls_only_required_visual_pages(monkeypatch, tmp_path: Path):
    source = tmp_path / "mixed.pdf"
    _make_pdf(source, ["Clean selectable source text with sufficient length. " * 8, ""])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    assert [page.route for page in record.pages] == ["native_text", "vision"]
    adapter = TrackingVision()

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        persisted = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"][0]

    assert response.status_code == 200
    assert adapter.calls == ["page:2/rendered"]
    completed = {
        image["source_location"] for image in persisted["images"]
        if image["ocr_status"] == "completed"
    }
    assert "page:2/rendered" in completed
    assert "page:1/rendered" not in completed


def test_single_page_visual_failure_degrades_instead_of_whole_document_502(monkeypatch, tmp_path: Path):
    """单页视觉失败不再把整份资料打成 502，但要留下两处痕迹（docs/209 §5）。

    第 2 页那一页的文字层不足采信（低于 32 字阈值）走了视觉，视觉又失败；
    第 1 页的正文仍然可用，所以本次分析按部分资料继续——这与
    ``wt-e2e-final`` 的 ``sample-c-rerun2``（失败页 [2]，整份 502）是同一个
    缺陷面：一页失败不该等于整份没有结果。

    但"第 2 页还剩几个字"**不是**可用正文：那几个字正是 profile 判过不足采信
    的噪声（``very_low_native_text``）。给用户看的话必须如实说这一页没可靠读到，
    不许说成"改用本地文字层继续分析"（``docs/209`` §3.4/§4）。
    """
    source = tmp_path / "degraded.pdf"
    _make_pdf(source, ["Clean selectable source text with sufficient length. " * 8, "Too short to trust"])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    assert [page.route for page in record.pages] == ["native_text", "vision"]
    assert record.pages[0].quality["label"] == "reliable"
    # 这一页"有字"，但那点字是产品自己判过不足采信的噪声。
    assert record.pages[1].text.strip()
    assert record.pages[1].quality["label"] in {"poor", "empty"}
    adapter = TrackingVision(
        fail_locations={"page:2/rendered"},
        fail_error=VisionAdapterError("vision_invalid_response", "视觉服务内容不是有效 JSON"),
    )

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        persisted = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"][0]

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    # 给人看的话：哪一页失败、这一页的正文从哪来，写得出来才算留痕。
    gap = next(text for text in body["gaps"] if "第 2 页" in text)
    assert "页面识别（OCR）失败" in gap
    assert "视觉服务返回的内容不是有效 JSON" in gap
    # 这一页的原生文字本来就不足采信，不许说成"改用本地文字层继续分析"。
    assert "原生文字不足采信" in gap
    assert "没有可靠读到" in gap
    assert "改用本地文字层" not in gap
    # 给排查用的话：类名、稳定错误码、脱敏原文、出错行，一样不少。
    failure = next(item for item in body["failures"] if item["page"] == 2)
    assert failure["type"] == "VisionAdapterError"
    assert failure["code"] == "vision_invalid_response"
    assert failure["error"] == "视觉服务内容不是有效 JSON"
    assert failure["source_location"] == "page:2/rendered"
    assert failure["location"]["line"] and failure["location"]["file"]
    # 两处分开写：异常类名与出错行不进给人看的缺口文字。
    assert "VisionAdapterError" not in gap
    assert "location" not in gap
    # 判据没有放宽：这一页确实没有视觉证据，失败状态如实落库。
    rendered = next(
        image for image in persisted["images"]
        if image["source_location"] == "page:2/rendered"
    )
    assert rendered["ocr_status"] == "failed"
    assert "page:2/rendered" not in persisted["model_extraction"]["fields"]
    assert adapter.calls == ["page:2/rendered"]


def test_visual_failure_without_any_body_text_still_fails_whole_document(monkeypatch, tmp_path: Path):
    """边界：视觉是这份资料**唯一**的正文来源时，整份失败（不把丢数据说成跳过）。"""
    source = tmp_path / "scan-only.pdf"
    _make_pdf(source, ["", ""])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    assert [page.route for page in record.pages] == ["vision", "vision"]
    assert not record.extracted_text.strip()
    adapter = TrackingVision(fail_locations={"page:1/rendered", "page:2/rendered"})

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "visual_page_analysis_failed",
        "message": "PDF 视觉分析未完成，失败页：[1, 2]",
    }
    assert "runtime-test-key" not in response.text


def test_untrusted_native_noise_alone_still_fails_whole_document(monkeypatch, tmp_path: Path):
    """每页都"有字"、但那些字正是 profile 判过不足采信的噪声时，视觉全失败仍整份 502。

    这是"有正文"判据最容易放宽的一处：``page.text`` 非空并不等于可信正文。
    一页被路由到 ``vision`` 本来就是产品判定它原生文字不足采信
    （``empty_native_text``／``very_low_native_text``／乱码／可疑字形，
    ``document_ingestion._assess_pdf_page``）；把这种残留字符当成"还有正文可读"
    会把丢数据说成跳过（``docs/209`` §3.4/§4）。
    """
    source = tmp_path / "noise-only.pdf"
    _make_pdf(source, ["Too short", "Page 12"])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    assert [page.route for page in record.pages] == ["vision", "vision"]
    assert all(page.text.strip() for page in record.pages)
    assert all(page.quality["label"] in {"poor", "empty"} for page in record.pages)
    adapter = TrackingVision(fail_locations={"page:1/rendered", "page:2/rendered"})

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "visual_page_analysis_failed",
        "message": "PDF 视觉分析未完成，失败页：[1, 2]",
    }


def test_locally_recovered_page_text_counts_as_usable_body_text(monkeypatch, tmp_path: Path):
    """本地结构化识别读到的正文算可信正文，视野视觉失败也不整份 502。

    载荷照 ``_parse_pdf``（``document_ingestion``）在本地识别成功时留下的样子造：
    region 文字 ≥32 字且无版面问题才写回页、才记 ``local_text_layer`` provenance，
    页的 ``route`` 仍是 ``vision``、``route_reason`` 是
    ``local_text_draft_pending_visual``，``quality.label`` 仍是最初那次的
    ``empty``（本地识别不重算 profile）。判据若只看 ``route``／``label`` 会把这
    一页判成"没有正文"，那是另一种误伤：扫描件本地已经读出来了，却因为付费视觉
    偶发失败而整份 502。
    """
    source = tmp_path / "local-draft.pdf"
    _make_pdf(source, [""])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    payload = record.to_dict()
    recovered = "本地识别读到的正文内容" * 4
    page = payload["pages"][0]
    page["text"] = recovered
    page["route_reason"] = "local_text_draft_pending_visual"
    page["provenance"].append({
        "kind": "local_text_layer", "parser": "rapidocr",
        "location": page["source_location"],
    })
    payload["extracted_text"] = recovered
    adapter = TrackingVision(
        fail_locations={"page:1/rendered"},
        fail_error=VisionAdapterError("vision_provider_unavailable", "视觉服务暂时不可用"),
    )

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, payload)
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    gap = next(text for text in body["gaps"] if "第 1 页" in text)
    assert "该页改用本地识别的文字继续分析" in gap
    assert "不足采信" not in gap
    assert "视觉服务暂时不可用" in gap


def test_risky_layout_page_text_still_counts_as_usable_body_text(monkeypatch, tmp_path: Path):
    """文字达标、只是版面复杂（``label=risky``）的页，视觉失败后仍算可信正文。

    ``_assess_pdf_page`` 把"文字本身没问题、但整页被大图压住"的页判成
    ``complex_layout_risk``／``risky``（``image_dominant_page``，``document_ingestion``
    :296-297、:339-340），并因此把页路由到 ``vision``。这类页的文字是可用的：
    它的 ``quality.signals`` 里没有任何文字类信号（不是 0 字／<32 字／乱码／可疑字形），
    所以判据必须认它，否则一页版面复杂＋视觉偶发失败就把整份资料打成 502。

    装置走的是产品真实存在的一条路：``pdf_vision_mode="all"``（``POST
    /tasks/{id}/documents/{id}/ocr`` 显式重读整份资料时用的就是它，``routes.py``
    :1035）。它把页强行路由到 ``vision``（:652-654）并跳过本地文字写回（:713），
    于是这一页落库时 ``label`` 仍是 profile 判的 ``risky``、provenance 里没有
    ``structured_pdf``／``local_text_layer``——判据的另一条不会命中，能把
    ``label == "risky"`` 这一条单独验出来。视觉随后失败（模拟重读没成）。
    """
    page_text = "Clean selectable source text with sufficient length."
    source = tmp_path / "risky-layout.pdf"
    _make_image_dominant_pdf(source, page_text)
    adapter = TrackingVision(
        fail_locations={"page:1/rendered"},
        fail_error=VisionAdapterError("vision_provider_unavailable", "视觉服务暂时不可用"),
    )
    record = DocumentIngestionService(
        tmp_path / "profile-storage", pdf_vision_mode="all", vision_adapter=adapter,
    ).ingest_file(source)
    assert [page.route for page in record.pages] == ["vision"]
    assert record.pages[0].quality["label"] == "risky"
    assert "image_dominant_page" in record.pages[0].quality["signals"]
    assert record.pages[0].text.strip() == page_text
    # 这一页不是靠本地识别 provenance 才被认成可信的。
    assert not {"structured_pdf", "local_text_layer"} & {
        item["kind"] for item in record.pages[0].provenance
    }
    assert adapter.calls == ["page:1/rendered"]
    adapter.calls = []

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    gap = next(text for text in body["gaps"] if "第 1 页" in text)
    # 这一页的文字是达标的：说成"改用原生文字层继续分析"，不说"不足采信"。
    assert f"该页改用原生文字层继续分析（{len(page_text)} 字）" in gap
    assert "不足采信" not in gap
    assert "视觉服务暂时不可用" in gap
    assert adapter.calls == ["page:1/rendered"]


def test_deferred_by_page_cap_page_is_not_trusted_body_text(monkeypatch, tmp_path: Path):
    """页数超限被推迟的页在 ``route`` 上是 ``native_text``，但那不是"可信正文"。

    ``_parse_pdf`` 在页数超过 ``LAB_VISION_MAX_PAGES`` 时，把没被选中付费识别的页
    ``route`` 翻成 ``native_text``、``route_reason = 'deferred_by_page_cap'``
    （``document_ingestion`` :749-760），判据只是 ``(page.text or '').strip()``——
    所以这些页里既有真的本地识别文字，也有纯噪声原文字符。判据若退化成看
    ``route == 'native_text'``，这些噪声页就会冒充正文，把"整份没有可靠读到"
    说成"跳过了一页"（``docs/209`` §3.4/§4）。

    这里两页都只有 9 个噪声字（``label=poor``），第 2 页因超限被推迟、``route`` 是
    ``native_text``；视觉在第 1 页失败后，这份资料没有任何可信正文，仍应整份 502。
    """
    source = tmp_path / "capped.pdf"
    _make_pdf(source, ["Too short", "Tiny page"])
    monkeypatch.setenv("LAB_VISION_MAX_PAGES", "1")
    adapter = TrackingVision(fail_locations={"page:1/rendered"})
    record = DocumentIngestionService(
        tmp_path / "profile-storage", vision_adapter=adapter,
    ).ingest_file(source)
    assert [page.route for page in record.pages] == ["vision", "native_text"]
    assert record.pages[1].route_reason == "deferred_by_page_cap"
    # 被推迟的页"有字"、`route` 也是 `native_text`，但那点字正是 profile 判过
    # 不足采信的噪声——所以判成"有正文"是错的。
    assert record.pages[1].text.strip()
    assert record.pages[1].quality["label"] == "poor"
    assert all(page.text.strip() for page in record.pages)
    assert adapter.calls == ["page:1/rendered"]
    adapter.calls = []

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "visual_page_analysis_failed",
        "message": "PDF 视觉分析未完成，失败页：[1]",
    }
    # 被推迟的页不再需要付费识别，也就不会被重新渲染。
    assert adapter.calls == ["page:1/rendered"]


def test_completed_visual_page_still_counts_when_another_page_fails(monkeypatch, tmp_path: Path):
    """扫描件里一页视觉成功、另一页失败：成功那一页就是可信正文，不该整份 502。

    这也是"可信正文"判据里"视觉成功页"那一条的必要性所在：纯扫描页 profile 标签是
    ``empty``、也没有本地识别 provenance，只看页级文字会把这两页都判成没正文。
    """
    source = tmp_path / "scan-partial.pdf"
    _make_pdf(source, ["", ""])
    record = DocumentIngestionService(tmp_path / "profile-storage").ingest_file(source)
    assert [page.route for page in record.pages] == ["vision", "vision"]
    adapter = TrackingVision(fail_locations={"page:2/rendered"})

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, record.to_dict())
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        persisted = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"][0]

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    gap = next(text for text in body["gaps"] if "第 2 页" in text)
    # 这一页一个字都没有：如实说没有读到，不说成"改用别的文字继续分析"。
    assert "该页没有任何可用正文" in gap
    assert "改用" not in gap
    # 这个 200 只能是"已有视觉成功页"那一句换来的：成功页的正文虽然写回了页，
    # 但页级判定仍是 `empty`、也没有本地识别 provenance，只看页级文字它同样是
    # "没有正文"——去掉那个子句，这条用例就会变回 502。
    recovered = persisted["pages"][0]
    assert recovered["text"].strip()
    assert recovered["quality"]["label"] == "empty"
    assert not {"structured_pdf", "local_text_layer"} & {
        item["kind"] for item in recovered["provenance"]
    }


def test_legacy_pdf_profile_migration_reuses_completed_visual_page(monkeypatch, tmp_path: Path):
    source = tmp_path / "legacy.pdf"
    _make_pdf(source, ["Clean selectable source text with sufficient length. " * 8, ""])
    initial_adapter = TrackingVision()
    record = DocumentIngestionService(
        tmp_path / "profile-storage", vision_adapter=initial_adapter,
    ).ingest_file(source)
    assert initial_adapter.calls == ["page:2/rendered"]
    legacy = record.to_dict()
    for page in legacy["pages"]:
        page.pop("route", None)
        page.pop("quality", None)
        page.pop("route_reason", None)
        page.pop("provenance", None)
    adapter = TrackingVision()

    with _open_app(monkeypatch, tmp_path, adapter) as client:
        task_id = _store_document(client, legacy)
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        persisted = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"][0]

    assert response.status_code == 200
    assert adapter.calls == []
    assert [page["route"] for page in persisted["pages"]] == ["native_text", "vision"]
    assert all(
        page["quality"]["profile_version"] == PDF_PAGE_PROFILE_VERSION
        for page in persisted["pages"]
    )
    rendered = next(
        image for image in persisted["images"]
        if image["source_location"] == "page:2/rendered"
    )
    assert rendered["ocr_status"] == "completed"
    assert persisted["model_extraction"]["fields"]["page:2/rendered"]["text"]
