"""M02 visual/OCR adapter boundary.

The adapter is deliberately provider-neutral. Real providers can implement
``extract`` later; tests use ``MockMultimodalOCRAdapter``. All returned fields
are persisted as pending-review drafts and never become confirmed facts.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from backend.schemas.documents import DocumentImage, DocumentPage, DocumentRecord, ModelExtraction, ParseWarning
from backend.services.vision import VisionResult


@dataclass(frozen=True)
class VisualInput:
    path: str
    media_type: str
    location: str
    page: int | None = None
    region: dict[str, float] | None = None


@dataclass
class OCRField:
    field_name: str
    value: Any
    unit: str | None = None
    location: str | None = None
    region: dict[str, float] | None = None
    confidence: float | None = None


@dataclass
class OCRResult:
    text: str = ""
    fields: list[OCRField] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)


class MultimodalOCRAdapter(Protocol):
    provider: str

    def extract(self, visual_input: VisualInput) -> OCRResult:
        """Read one image/page and return source-located draft text/fields."""


class MockMultimodalOCRAdapter:
    """Deterministic adapter for the LS-50HD regression and unit tests."""

    provider = "mock-multimodal"

    def __init__(self, results: dict[str, OCRResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[VisualInput] = []

    def extract(self, visual_input: VisualInput) -> OCRResult:
        self.calls.append(visual_input)
        if visual_input.location in self.results:
            return self.results[visual_input.location]
        return OCRResult(
            text="江阴滨江医疗设备有限公司 LS-50HD 立式压力蒸汽灭菌器",
            fields=[
                OCRField("model", "LS-50HD", location=visual_input.location, region={"x": 0.10, "y": 0.08, "width": 0.35, "height": 0.08}),
                OCRField("capacity", 50, "L", visual_input.location, {"x": 0.52, "y": 0.26, "width": 0.18, "height": 0.05}),
                OCRField("pressure", 0.3, "MPa", visual_input.location, {"x": 0.52, "y": 0.38, "width": 0.18, "height": 0.05}),
                OCRField("temperature", 150, "℃", visual_input.location, {"x": 0.52, "y": 0.48, "width": 0.18, "height": 0.05}),
            ],
            source_refs=[{"location": visual_input.location, "region": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}, "quote": "双页扫描资料视觉识别结果"}],
        )

    def analyze(self, image_bytes: bytes, *, media_type: str, source_location: str) -> VisionResult:
        result = self.extract(VisualInput("<bytes>", media_type, source_location))
        fields = {item.field_name: {"value": item.value, "unit": item.unit, "region": item.region} for item in result.fields}
        return VisionResult(text=result.text, fields=fields, source_refs=result.source_refs, provider=self.provider)


def _media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class MultimodalOCRService:
    def __init__(self, storage_dir: str | Path, adapter: MultimodalOCRAdapter) -> None:
        self.storage_dir = Path(storage_dir)
        self.adapter = adapter

    def extract(self, record: DocumentRecord) -> DocumentRecord:
        inputs = self.prepare_inputs(record)
        if not inputs:
            warning = ParseWarning("no_visual_input", "资料没有可供视觉识别的图片或 PDF 页面", severity="error")
            record.warnings.append(warning)
            record.model_extraction = ModelExtraction(
                status="failed", provider=getattr(self.adapter, "provider", type(self.adapter).__name__),
                review_status="pending_review", warnings=[warning]
            )
            record.ocr_status = "failed"
            return record
        extraction = ModelExtraction(status="completed", provider=getattr(self.adapter, "provider", type(self.adapter).__name__), review_status="pending_review")
        extraction.artifacts = [{"path": item.path, "media_type": item.media_type, "location": item.location, "page": item.page} for item in inputs]
        try:
            for visual_input in inputs:
                result = self.adapter.extract(visual_input)
                if not result.text.strip() and not result.fields:
                    raise ValueError(f"视觉适配器返回空结果：{visual_input.location}")
                extraction.text = "\n".join(x for x in (extraction.text, result.text) if x)
                target = next((image for image in record.images
                               if (image.source_location or "").startswith(visual_input.location)), None)
                if target is not None:
                    target.ocr_text = result.text
                    target.structured_fields = {}
                    target.ocr_status = "completed"
                for field in result.fields:
                    location = field.location or visual_input.location
                    value = {
                        "value": field.value, "unit": field.unit, "status": "pending_review",
                        "location": location, "region": field.region, "confidence": field.confidence,
                    }
                    extraction.fields[field.field_name] = value
                    if target is not None:
                        target.structured_fields[field.field_name] = value
                refs = result.source_refs or [{"location": visual_input.location, "quote": result.text}]
                for ref in refs:
                    extraction.source_refs.append({
                        **ref,
                        "location": ref.get("location") or visual_input.location,
                        "document_id": record.id,
                        "page": visual_input.page,
                    })
                if target is not None and result.text:
                    if visual_input.page is not None:
                        page = next((p for p in record.pages if p.number == visual_input.page), None)
                        if page is not None and not page.text.strip():
                            page.text = result.text
                    elif not record.pages:
                        record.pages.append(DocumentPage(1, result.text, visual_input.location, record.id))
            record.model_extraction = extraction
            record.model_extraction.review_status = "pending_review"
            record.ocr_status = "completed"
            for image in record.images:
                if image.ocr_status in {"not_requested", "pending"}:
                    image.ocr_status = "completed"
            record.extracted_text = "\n\n".join(p.text for p in record.pages if p.text.strip()) or extraction.text
        except Exception as exc:
            extraction.status = "failed"
            extraction.warnings.append(ParseWarning("multimodal_extraction_failed", f"多模态识别失败：{exc}", severity="error"))
            record.model_extraction = extraction
            record.ocr_status = "failed"
            for image in record.images:
                image.ocr_status = "failed"
        return record

    def prepare_inputs(self, record: DocumentRecord) -> list[VisualInput]:
        if record.document_type == "image" and record.original_path:
            return [VisualInput(record.original_path, record.media_type or _media_type(Path(record.original_path)), "image:1")]
        if record.document_type != "pdf" or not record.original_path:
            return []
        return self._render_pdf_pages(record)

    def _render_pdf_pages(self, record: DocumentRecord) -> list[VisualInput]:
        try:
            import fitz  # type: ignore
        except ImportError:
            record.warnings.append(ParseWarning("dependency_missing", "扫描 PDF 页面渲染需要安装 PyMuPDF", severity="error"))
            return []
        output_dir = self.storage_dir / "visual_inputs" / record.id
        output_dir.mkdir(parents=True, exist_ok=True)
        inputs: list[VisualInput] = []
        pdf = fitz.open(record.original_path)
        for index, page in enumerate(pdf, 1):
            page_text = ""
            if index <= len(record.pages):
                page_text = record.pages[index - 1].text or ""
            has_embedded_images = any((image.source_location or "").startswith(f"page:{index}/image:") for image in record.images)
            # Do not send selectable text-only pages to a vision model.  This
            # keeps costs bounded and avoids replacing source text with OCR.
            if page_text.strip() and not has_embedded_images:
                continue
            output = output_dir / f"page-{index}.png"
            if not output.exists():
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                pixmap.save(str(output))
            location = f"page:{index}"
            inputs.append(VisualInput(str(output), "image/png", location, page=index))
            record.images.append(DocumentImage(len(record.images) + 1, f"page:{index}/render", "image/png", hashlib.sha256(output.read_bytes()).hexdigest(), "pending"))
        pdf.close()
        return inputs


__all__ = ["VisualInput", "OCRField", "OCRResult", "MultimodalOCRAdapter", "MockMultimodalOCRAdapter", "MultimodalOCRService"]
