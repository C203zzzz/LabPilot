"""M02 document ingestion.

The parser preserves source material and parser output as different fields. It
never turns model output into a verified fact; model extraction is opt-in and
defaults to ``not_run``.
"""
from __future__ import annotations

import csv
import copy
import hashlib
import html.parser
import io
import mimetypes
import os
import re
import unicodedata
import uuid
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import urlparse

from backend.schemas.documents import (
    DocumentImage, DocumentPage, DocumentRecord, DocumentTable, ParseWarning,
)
from backend.services.vision import VisionAdapter, normalize_vision_result


IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".json": "application/json", ".xml": "application/xml"}
PdfVisionMode = Literal["adaptive", "all"]

# Conservative, category-independent routing thresholds.  They mirror the
# cascade used by mature document parsers: trust a healthy PDF text layer,
# escalate damaged pages, and use layout evidence only when it crosses an
# explicit risk gate.
_MIN_NATIVE_TEXT_CHARS = 32
_MAX_SUSPICIOUS_CHAR_RATIO = 0.05
_MIN_SEMANTIC_CHAR_RATIO = 0.25
_LAYOUT_RISK_THRESHOLD = 2
PDF_PAGE_PROFILE_VERSION = 5

#: Words that mark a page as carrying the model's own specification or the
#: configuration under purchase. Used only to order pages, never to conclude
#: anything about them.
_PARAMETER_KEYWORDS = re.compile(
    r'型号|规格|参数|技术指标|配置|选型|订货|货号|转速|离心力|容量|功率|电压|频率'
    r'|温度|湿度|精度|尺寸|重量|净重|噪声|材质|接口|软件|附件|转子|适配器'
    r'|model|specification|technical\s+data|capacity|speed|power|voltage|dimension|weight',
    re.I,
)
_MODEL_PATTERN = re.compile(r'[A-Za-z]{1,6}[- ]?\d{2,5}[A-Za-z]{0,4}')
DEFAULT_VISION_PAGE_CAP = 20


def _vision_page_cap() -> int:
    """How many pages one round may render remotely (``LAB_VISION_MAX_PAGES``).

    Read per call, not at import, so an operator can widen or disable the cap
    (``0`` = unlimited) without restarting the service.
    """
    raw = os.getenv('LAB_VISION_MAX_PAGES')
    if raw is None or not str(raw).strip():
        return DEFAULT_VISION_PAGE_CAP
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_VISION_PAGE_CAP


def _geometric_cells(table, rows):
    """Map actual PDF rectangles to grid spans, without filling guessed blanks."""
    xs = sorted({round(v, 2) for cell in table.cells for v in (cell[0], cell[2])})
    ys = sorted({round(v, 2) for cell in table.cells for v in (cell[1], cell[3])})
    if len(xs) - 1 != max(map(len, rows), default=0) or len(ys) - 1 != len(rows):
        return []
    result = []
    for box in table.cells:
        c0, c1 = xs.index(round(box[0], 2)), xs.index(round(box[2], 2))
        r0, r1 = ys.index(round(box[1], 2)), ys.index(round(box[3], 2))
        result.append({'row_start': r0, 'row_end': r1, 'col_start': c0, 'col_end': c1,
                       'text': rows[r0][c0], 'bbox': list(box)})
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _warning(code: str, message: str, location: str | None = None, severity: str = "warning") -> ParseWarning:
    return ParseWarning(code=code, message=message, location=location, severity=severity)  # type: ignore[arg-type]


def _pdf_page_counts(record: DocumentRecord, reader: Any, layout_pdf: Any,
                     pages_before: int) -> str:
    """说清一次整份 PDF 解析中断时"已解析几页、整份几页"。

    ``docs/209`` §3.4：对外的粒度不许粗于内部事实。``pdf_parse_failed`` 是
    ``_parse_pdf`` 整份失败的唯一出口，原来只说"解析失败"——连缺了几页都没记，
    所以这条记录在排查时既不能降级判级、也说不清影响面。两个数能拿多少拿多少：
    ``PdfReader`` 构造失败时页清单根本不存在，只能写"未能获取"；
    ``reader.pages`` 惰性解析中断时，总数与已解析数都还是可查的事实。
    """
    parsed = max(0, len(record.pages) - pages_before)
    total: int | None = None
    if reader is not None:
        try:
            total = int(len(reader.pages))
        except Exception:
            total = None
    if total is None and layout_pdf is not None:
        # 页循环的 finally 已经把这份文档关掉了，关掉的文档取页数会抛错；
        # 这时退回"未能获取"，不编一个数出来。
        try:
            total = int(layout_pdf.page_count)
        except Exception:
            total = None
    if total is None:
        return f"已解析 {parsed} 页，整份总页数未能获取（页清单未建立）"
    suffix = "，其余页面未进入页清单" if parsed < total else ""
    return f"已解析 {parsed} 页，整份共 {total} 页{suffix}"


def _vision_failure(exc: BaseException, source_location: str | None) -> dict[str, Any]:
    """把一次视觉调用失败记成 ``run['failures']`` 那一套形状的事实。

    给排查用的三样东西缺一不可（``docs/209`` §5）：异常**类名**、**脱敏后的原文**、
    **出错行**。 ``source_location`` 让这条记录回到具体那一页。原文按
    ``procurement_research.redact_error_text`` 脱敏后再落库——视觉端点的异常里
    可能带着整条请求地址与它的查询串，而这份记录会随文档一起持久化。
    """
    from backend.services.procurement_research import failure_location, redact_error_text
    return {
        'stage': 'pdf_vision',
        'type': type(exc).__name__,
        'code': str(getattr(exc, 'code', '') or 'vision_adapter_failed'),
        'location': failure_location(exc),
        'source_location': source_location,
        'error': redact_error_text(exc),
    }


class _HTMLText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_title = tag.lower() == "title"

    def handle_endtag(self, tag: str) -> None:
        self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif data.strip():
            self.parts.append(data.strip())


class DocumentIngestionService:
    def __init__(self, storage_dir: str | Path = "storage", *,
                 vision_adapter: VisionAdapter | None = None,
                 pdf_vision_mode: PdfVisionMode = "adaptive") -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Provider SDKs and credentials stay outside M02.  Passing an adapter
        # explicitly is the only way to request visual/OCR analysis.
        self.vision_adapter = vision_adapter
        if pdf_vision_mode not in {"adaptive", "all"}:
            raise ValueError("pdf_vision_mode must be 'adaptive' or 'all'")
        self.pdf_vision_mode = pdf_vision_mode

    def ingest_file(self, path: str | Path, *, document_id: str | None = None,
                    vision_adapter: VisionAdapter | None = None,
                    pdf_vision_mode: PdfVisionMode | None = None) -> DocumentRecord:
        source = Path(path)
        data = source.read_bytes()
        record = self._base(document_id, source.name, str(source), data, source.suffix.lower(), None)
        original = self.storage_dir / "originals" / f"{record.sha256}{source.suffix.lower()}"
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists():
            original.write_bytes(data)
        else:
            record.warnings.append(_warning("duplicate_source", "内容哈希已存在，复用同一份原始资料副本", str(original)))
        record.original_path = str(original)
        self._parse_bytes(
            record, data, source.suffix.lower(), vision_adapter or self.vision_adapter,
            pdf_vision_mode or self.pdf_vision_mode,
        )
        return record

    def ingest_url(self, url: str, *, document_id: str | None = None, timeout: float = 15.0) -> DocumentRecord:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return DocumentRecord(id=document_id or str(uuid.uuid4()), source_name=url, source_uri=url, document_type="webpage", media_type=None, sha256=None, size_bytes=None, original_path=None, warnings=[_warning("invalid_url", "网页链接必须是 http 或 https URL", url, "error")])
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LabInstrumentSelector/0.1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                media_type = response.headers.get_content_type()
        except Exception as exc:
            return DocumentRecord(id=document_id or str(uuid.uuid4()), source_name=parsed.path.rsplit("/", 1)[-1] or url, source_uri=url, document_type="webpage", media_type=None, sha256=None, size_bytes=None, original_path=None, warnings=[_warning("fetch_failed", f"网页抓取失败：{exc}", url, "error")])
        record = self._base(document_id, parsed.path.rsplit("/", 1)[-1] or parsed.netloc, url, data, ".html", media_type)
        record.document_type = "webpage"
        original = self.storage_dir / "originals" / f"{record.sha256}.html"
        original.parent.mkdir(parents=True, exist_ok=True)
        if not original.exists():
            original.write_bytes(data)
        record.original_path = str(original)
        parser = _HTMLText()
        try:
            parser.feed(data.decode("utf-8", errors="replace"))
            text = "\n".join(parser.parts)
            record.pages = [DocumentPage(1, text, url, record.id)]
            record.extracted_text = text
        except Exception as exc:
            record.warnings.append(_warning("html_parse_failed", f"网页正文解析失败：{exc}", url, "error"))
        return record

    def _base(self, document_id: str | None, name: str, uri: str, data: bytes, suffix: str, media_type: str | None) -> DocumentRecord:
        dtype = "image" if suffix in IMAGE_TYPES else "pdf" if suffix == ".pdf" else "spreadsheet" if suffix in {".xlsx", ".xls", ".xlsm"} else "csv" if suffix == ".csv" else "text" if suffix in TEXT_TYPES else "unknown"
        return DocumentRecord(id=document_id or str(uuid.uuid4()), source_name=name, source_uri=uri, document_type=dtype, media_type=media_type or IMAGE_TYPES.get(suffix) or TEXT_TYPES.get(suffix) or mimetypes.guess_type(name)[0], sha256=_sha256(data), size_bytes=len(data), original_path=str(uri))

    def _parse_bytes(self, record: DocumentRecord, data: bytes, suffix: str,
                     vision_adapter: VisionAdapter | None = None,
                     pdf_vision_mode: PdfVisionMode = "adaptive") -> None:
        if record.document_type == "text":
            text = data.decode("utf-8", errors="replace")
            record.pages = [DocumentPage(1, text, "page:1", record.id)]
            record.extracted_text = text
        elif record.document_type == "csv":
            self._parse_csv(record, data)
        elif record.document_type == "spreadsheet":
            self._parse_spreadsheet(record, data)
        elif record.document_type == "image":
            self._parse_image(record, data, vision_adapter)
        elif record.document_type == "pdf":
            self._parse_pdf(record, data, vision_adapter, pdf_vision_mode)
        else:
            record.warnings.append(_warning("unsupported_format", f"暂不支持的资料格式：{suffix or '无扩展名'}", severity="error"))

    def _parse_csv(self, record: DocumentRecord, data: bytes) -> None:
        try:
            text = data.decode("utf-8-sig", errors="replace")
            rows = [list(row) for row in csv.reader(io.StringIO(text))]
            record.tables = [DocumentTable(1, rows, source_location="sheet:1")]
            record.pages = [DocumentPage(1, "\n".join(",".join(row) for row in rows), "sheet:1", record.id)]
            record.extracted_text = record.pages[0].text
        except Exception as exc:
            record.warnings.append(_warning("csv_parse_failed", f"CSV 解析失败：{exc}", severity="error"))

    def _parse_spreadsheet(self, record: DocumentRecord, data: bytes) -> None:
        try:
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            for idx, ws in enumerate(wb.worksheets, 1):
                rows = [["" if value is None else str(value) for value in row] for row in ws.iter_rows(values_only=True)]
                record.tables.append(DocumentTable(idx, rows, ws.title, f"sheet:{ws.title}"))
                record.pages.append(DocumentPage(idx, "\n".join("\t".join(row) for row in rows), f"sheet:{ws.title}", record.id))
            record.extracted_text = "\n\n".join(page.text for page in record.pages)
        except ImportError:
            record.warnings.append(_warning("dependency_missing", "解析 Excel 需要安装 openpyxl；原始文件哈希和路径仍已保留", severity="error"))
        except Exception as exc:
            record.warnings.append(_warning("spreadsheet_parse_failed", f"Excel 解析失败：{exc}", severity="error"))

    @staticmethod
    def _looks_like_recoverable_mojibake(text: str) -> bool:
        """Detect common UTF-8 decoded as a single-byte encoding.

        The repair is used only as a quality signal; source text is never
        silently rewritten.
        """
        original_cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        for encoding in ("latin-1", "cp1252"):
            try:
                repaired = text.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            repaired_cjk = sum("\u3400" <= char <= "\u9fff" for char in repaired)
            if repaired_cjk >= 2 and repaired_cjk > original_cjk * 2:
                return True
        return False

    @classmethod
    def _assess_pdf_page(cls, text: str, layout: dict[str, Any] | None = None) -> tuple[dict[str, Any], str, str]:
        layout = dict(layout or {})
        compact = "".join(char for char in text if not char.isspace())
        char_count = len(compact)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        suspicious_count = 0
        semantic_count = 0
        for char in compact:
            codepoint = ord(char)
            category = unicodedata.category(char)
            private_use = (
                0xE000 <= codepoint <= 0xF8FF
                or 0xF0000 <= codepoint <= 0xFFFFD
                or 0x100000 <= codepoint <= 0x10FFFD
            )
            if char == "\ufffd" or private_use or category in {"Cc", "Cs", "Cn"}:
                suspicious_count += 1
            if category[:1] in {"L", "N"}:
                semantic_count += 1
        suspicious_ratio = suspicious_count / max(char_count, 1)
        semantic_ratio = semantic_count / max(char_count, 1)
        mojibake = cls._looks_like_recoverable_mojibake(compact) if char_count >= _MIN_NATIVE_TEXT_CHARS else False

        image_count = int(layout.get("image_count") or 0)
        text_block_count = int(layout.get("text_block_count") or len(lines))
        image_coverage = float(layout.get("image_coverage") or 0.0)
        text_coverage = float(layout.get("text_coverage") or 0.0)
        overlap_ratio = float(layout.get("text_overlap_ratio") or 0.0)
        drawing_count = int(layout.get("drawing_count") or 0)
        table_count = int(layout.get("table_count") or 0)
        short_line_ratio = (
            sum(len(line) <= 40 for line in lines) / len(lines) if lines else 0.0
        )

        layout_signals: list[str] = []
        layout_risk_score = 0
        if image_coverage >= 0.45:
            layout_signals.append("image_dominant_page")
            layout_risk_score += 2
        elif image_count >= 3 and image_coverage >= 0.15 and text_block_count >= 6:
            layout_signals.append("mixed_image_text_blocks")
            layout_risk_score += 1
        if overlap_ratio >= 0.12 and text_block_count >= 4:
            layout_signals.append("overlapping_text_blocks")
            layout_risk_score += 2
        # A successfully extracted table is already represented as its own
        # element, so vector grid lines alone should not trigger visual work.
        if table_count == 0 and drawing_count >= 25 and len(lines) >= 8 and short_line_ratio >= 0.50:
            layout_signals.append("dense_vector_layout")
            layout_risk_score += 1
        if (
            text_block_count >= 20
            and short_line_ratio >= 0.70
            and (text_coverage < 0.08 or char_count < 300)
        ):
            layout_signals.append("fragmented_text_layout")
            layout_risk_score += 1
        if layout_signals and text_coverage and text_coverage < 0.03:
            layout_signals.append("low_text_area_coverage")
            layout_risk_score += 1

        text_signals: list[str] = []
        if char_count == 0:
            text_signals.append("empty_native_text")
        elif char_count < _MIN_NATIVE_TEXT_CHARS:
            text_signals.append("very_low_native_text")
        if suspicious_ratio >= _MAX_SUSPICIOUS_CHAR_RATIO:
            text_signals.append("suspicious_glyph_ratio")
        if char_count >= _MIN_NATIVE_TEXT_CHARS and semantic_ratio < _MIN_SEMANTIC_CHAR_RATIO:
            text_signals.append("low_semantic_character_ratio")
        if mojibake:
            text_signals.append("recoverable_mojibake_pattern")

        if "empty_native_text" in text_signals:
            route, reason, label = "vision", "empty_native_text", "empty"
        elif "very_low_native_text" in text_signals:
            route, reason, label = "vision", "very_low_native_text", "poor"
        elif len(text_signals) > 0:
            route, reason, label = "vision", "corrupt_native_text", "poor"
        elif layout_risk_score >= _LAYOUT_RISK_THRESHOLD:
            route, reason, label = "vision", "complex_layout_risk", "risky"
        else:
            route, reason, label = "native_text", "native_text_reliable", "reliable"

        score = 1.0
        if label == "empty":
            score = 0.0
        elif label == "poor":
            score = max(0.05, 1.0 - max(suspicious_ratio * 4, 0.75 if char_count < _MIN_NATIVE_TEXT_CHARS else 0.45))
        elif label == "risky":
            score = 0.55
        quality = {
            "profile_version": PDF_PAGE_PROFILE_VERSION,
            "label": label,
            "score": round(score, 3),
            "char_count": char_count,
            "line_count": len(lines),
            "semantic_char_ratio": round(semantic_ratio, 4),
            "suspicious_char_ratio": round(suspicious_ratio, 4),
            "mojibake_detected": mojibake,
            "image_count": image_count,
            "image_coverage": round(image_coverage, 4),
            "text_block_count": text_block_count,
            "text_coverage": round(text_coverage, 4),
            "text_overlap_ratio": round(overlap_ratio, 4),
            "drawing_count": drawing_count,
            "table_count": table_count,
            "layout_risk_score": layout_risk_score,
            "signals": text_signals + layout_signals,
        }
        return quality, route, reason

    @staticmethod
    def _vision_page_relevance(page: DocumentPage) -> int:
        """Rank a vision page by how likely it carries model/parameter data.

        Local text only: a page whose text could be recovered locally is scored
        by what that text says, so no paid call is spent deciding what to pay
        for. A page with no readable local text is a pure image; it cannot be
        scored on content and keeps page order as its tie-breaker.
        """
        text = page.text or ''
        if not text.strip():
            return 0
        compact = re.sub(r'\s+', '', text)
        # A page with *some* readable text is always a better candidate than a
        # page we could not read at all.
        score = 1
        score += 3 * len(_PARAMETER_KEYWORDS.findall(text))
        score += 2 * len(_MODEL_PATTERN.findall(text))
        score += min(len(re.findall(r'\d', compact)) // 10, 8)
        score += min(len(re.findall(r'\d+(?:\.\d+)?\s*(?:r/min|rpm|×g|ml|mL|L|kg|mm|℃|°C|%|kW|W|Hz|A|V|min|sec|h)\b', text, re.I)), 6)
        signals = set((page.quality or {}).get('signals') or [])
        if 'image_dominant_page' in signals:
            score += 2
        if 'table_count' in (page.quality or {}) and (page.quality or {}).get('table_count'):
            score += 2
        return score

    def _bounded_vision_pages(self, record: DocumentRecord, required_pages: set[int]) -> tuple[set[int], set[int]]:
        """Order the pages that need vision and cap how many may be rendered.

        An unbounded page list is what turns a long manual into hours of serial
        paid calls. The cap keeps the most relevant pages this round; the rest
        stay auditable (``deferred_by_page_cap``) instead of silently vanishing,
        and ``analyze_pdf_pages`` can still retry any explicit page later.
        """
        if not required_pages:
            return set(), set()
        cap = _vision_page_cap()
        by_number = {page.number: page for page in record.pages}
        ranked = sorted(required_pages, key=lambda number: (
            -self._vision_page_relevance(by_number[number]) if number in by_number else 0,
            number,
        ))
        if cap <= 0 or len(ranked) <= cap:
            return set(required_pages), set()
        return set(ranked[:cap]), set(ranked[cap:])

    @staticmethod
    def has_current_pdf_profile(record: DocumentRecord) -> bool:
        """Return whether every PDF page has an explicit current profile."""
        return bool(record.pages) and not any(w.code == 'pdf_parse_failed' for w in record.warnings) and all(
            page.route in {"native_text", "vision"}
            and page.route_reason is not None
            and page.quality.get("profile_version") == PDF_PAGE_PROFILE_VERSION
            and bool(page.provenance)
            for page in record.pages
        )

    @staticmethod
    def required_vision_pages(record: DocumentRecord) -> set[int]:
        return {page.number for page in record.pages if page.route == "vision"}

    def rebuild_pdf_profile(self, record: DocumentRecord) -> DocumentRecord:
        """Migrate a persisted PDF record without repeating completed vision.

        Native text, layout metrics, and tables are rebuilt from the immutable
        original. Existing rendered-page evidence is then merged by source
        location. This prevents dataclass defaults on legacy pages from being
        mistaken for an intentional ``native_text`` route.
        """
        if record.document_type != "pdf" or not record.original_path:
            raise ValueError("record must be a persisted PDF")
        source = Path(record.original_path)
        data = source.read_bytes()
        refreshed = self._base(
            record.id,
            record.source_name,
            record.source_uri,
            data,
            ".pdf",
            record.media_type,
        )
        refreshed.original_path = record.original_path
        refreshed.category = record.category
        self._parse_pdf(refreshed, data, None, "adaptive")

        old_pages = {page.number: page for page in record.pages}
        rendered_images = [
            copy.deepcopy(image) for image in record.images
            if re.fullmatch(r"page:\d+/rendered", str(image.source_location or ""))
        ]
        refreshed.images.extend(rendered_images)
        for index, image in enumerate(refreshed.images, 1):
            image.index = index

        refreshed.model_extraction = copy.deepcopy(record.model_extraction)
        refreshed.ocr_status = record.ocr_status
        refreshed.warnings = [copy.deepcopy(w) for w in record.warnings if w.code != 'pdf_parse_failed'] + refreshed.warnings
        rendered_by_page: dict[int, DocumentImage] = {}
        for image in rendered_images:
            match = re.fullmatch(r"page:(\d+)/rendered", str(image.source_location or ""))
            if match:
                rendered_by_page[int(match.group(1))] = image

        for page in refreshed.pages:
            old_page = old_pages.get(page.number)
            image = rendered_by_page.get(page.number)
            if image is None:
                continue
            old_visual_provenance = [
                copy.deepcopy(item) for item in (old_page.provenance if old_page else [])
                if item.get("kind") == "visual_page_analysis"
            ]
            page.provenance.extend(old_visual_provenance or [{
                "kind": "visual_page_analysis",
                "location": image.source_location,
                "status": image.ocr_status,
                "provider": refreshed.model_extraction.provider,
                "sha256": image.sha256,
                "migrated": True,
            }])
            if image.ocr_status == "completed":
                if not page.text.strip() and old_page is not None and old_page.text.strip():
                    page.text = old_page.text
                location = image.source_location or f"page:{page.number}/rendered"
                migrated_text = image.ocr_text.strip()
                if not migrated_text and old_page is not None and old_page.text.strip():
                    migrated_text = old_page.text
                if location not in refreshed.model_extraction.fields and (
                    migrated_text or image.structured_fields
                ):
                    refreshed.model_extraction.fields[location] = {
                        "text": migrated_text,
                        "fields": copy.deepcopy(image.structured_fields),
                        "source_location": location,
                        "status": "pending_review",
                    }
        refreshed.extracted_text = "\n\n".join(
            page.text for page in refreshed.pages if page.text.strip()
        )
        return refreshed

    @staticmethod
    def _fitz_layout_metrics(page: Any) -> dict[str, Any]:
        """Collect cheap geometry signals without rendering the page."""
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        text_boxes: list[tuple[float, float, float, float]] = []
        image_area = 0.0
        image_count = 0
        try:
            import fitz
            blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES).get("blocks", [])
            # Geometry never needs decoded embedded bitmap bytes. Large scans
            # otherwise allocate full images just to obtain their bounding box.
            blocks += [{'type': 1, 'bbox': info['bbox']} for info in page.get_image_info()]
        except Exception:
            blocks = []
        for block in blocks:
            bbox = block.get("bbox") or ()
            if len(bbox) != 4:
                continue
            x0, y0, x1, y1 = (float(value) for value in bbox)
            area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            if block.get("type") == 1:
                image_count += 1
                image_area += area
            elif block.get("type") == 0:
                text_boxes.append((x0, y0, x1, y1))
        text_area = sum(max(0.0, x1 - x0) * max(0.0, y1 - y0) for x0, y0, x1, y1 in text_boxes)
        overlap_pairs = 0
        compared_pairs = 0
        for index, first in enumerate(text_boxes[:200]):
            for second in text_boxes[index + 1:200]:
                compared_pairs += 1
                overlap_width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
                overlap_height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
                if overlap_width * overlap_height > 1.0:
                    overlap_pairs += 1
        try:
            drawing_count = len(page.get_drawings())
        except Exception:
            drawing_count = 0
        return {
            "text_block_count": len(text_boxes),
            "image_count": image_count,
            "text_coverage": min(1.0, text_area / page_area),
            "image_coverage": min(1.0, image_area / page_area),
            "text_overlap_ratio": overlap_pairs / max(compared_pairs, 1),
            "drawing_count": drawing_count,
        }

    def _extract_pdf_tables(self, record: DocumentRecord, data: bytes) -> dict[int, int]:
        """Extract tables as independent elements when pdfplumber is present."""
        counts: dict[int, int] = {}
        try:
            import pdfplumber  # type: ignore
        except ImportError:
            record.warnings.append(_warning(
                "pdf_table_dependency_missing",
                "未安装可选依赖 pdfplumber，已保留原生文本并跳过表格元素提取",
            ))
            return counts
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page_number, page in enumerate(pdf.pages, 1):
                    try:
                        tables = page.find_tables() or []
                    except Exception as exc:
                        record.warnings.append(_warning(
                            "pdf_table_parse_failed", f"PDF 表格提取失败：{exc}",
                            f"page:{page_number}",
                        ))
                        continue
                    page_table_index = 0
                    for table in tables:
                        raw_rows = table.extract()
                        rows = [
                            ["" if value is None else str(value).strip() for value in row]
                            for row in (raw_rows or [])
                        ]
                        if not rows or not any(any(cell for cell in row) for row in rows):
                            continue
                        page_table_index += 1
                        record.tables.append(DocumentTable(
                            len(record.tables) + 1,
                            rows,
                            source_location=f"page:{page_number}/table:{page_table_index}",
                            cells=_geometric_cells(table, rows),
                        ))
                    if page_table_index:
                        counts[page_number] = page_table_index
        except Exception as exc:
            record.warnings.append(_warning(
                "pdf_table_parse_failed", f"PDF 表格提取失败：{exc}",
            ))
        return counts

    def _parse_pdf(self, record: DocumentRecord, data: bytes,
                   vision_adapter: VisionAdapter | None = None,
                   pdf_vision_mode: PdfVisionMode = "adaptive") -> None:
        pages_before = len(record.pages)
        # 先绑成 None：解析可能在任何一行失败，而 except 分支要拿它们报页数，
        # 不能留一个未绑定的名字。
        reader = None
        layout_pdf = None
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(data))
            table_counts = self._extract_pdf_tables(record, data)
            try:
                import fitz  # type: ignore
                layout_pdf = fitz.open(stream=data, filetype="pdf")
            except Exception:
                layout_pdf = None
            page_texts: list[str] = []
            page_has_images: list[bool] = []
            try:
                for idx, page in enumerate(reader.pages, 1):
                    try:
                        text = page.extract_text() or ""
                    except Exception:
                        text = ''
                        record.warnings.append(_warning('pdf_page_text_failed', '此页原生文本读取失败，保留页码并转本地结构解析/视觉复核', f'page:{idx}'))
                    page_texts.append(text)
                    # Keep an explicit image location even when an optional PDF
                    # image decoder is unavailable; downstream OCR can revisit it.
                    # ``page.images`` decodes every payload when iterated.
                    # Keys are enough for provenance and avoid paying that
                    # cost (or failing on malformed masks) before routing.
                    try:
                        embedded_names = [str(name) for name in page.images.keys()]  # pypdf >= 3
                    except Exception:
                        embedded_names = []
                    page_has_images.append(bool(embedded_names))
                    layout = {
                        "image_count": len(embedded_names),
                        "table_count": table_counts.get(idx, 0),
                    }
                    if layout_pdf is not None and idx <= layout_pdf.page_count:
                        try:
                            layout.update(self._fitz_layout_metrics(layout_pdf.load_page(idx - 1)))
                        except Exception:
                            record.warnings.append(_warning('pdf_page_layout_failed', '此页布局读取未完成，保留页码待复核', f'page:{idx}'))
                        layout["table_count"] = table_counts.get(idx, 0)
                    quality, route, reason = self._assess_pdf_page(text, layout)
                    if pdf_vision_mode == "all":
                        route = "vision"
                        reason = "forced_all_pages"
                    record.pages.append(DocumentPage(
                        idx, text, f"page:{idx}", record.id,
                        route=route, quality=quality, route_reason=reason,
                        provenance=[{
                            "kind": "native_pdf_text",
                            "location": f"page:{idx}",
                            "status": "available" if text.strip() else "empty",
                            "parser": "pypdf",
                            "char_count": quality["char_count"],
                        }],
                    ))
                    for image_name in embedded_names:
                        record.images.append(DocumentImage(
                            len(record.images) + 1,
                            f"page:{idx}/image:{image_name}",
                            "application/octet-stream",
                            None,
                            "not_requested",
                        ))
            finally:
                if layout_pdf is not None:
                    layout_pdf.close()
            # Keep native block coordinates even when optional Docling is unavailable.
            if record.original_path and any(p.route == "vision" for p in record.pages):
                structured_pages = [p.number for p in record.pages if p.route == 'vision']
                local_pass = 'none'
                try:
                    from backend.services.structured_pdf import parse_document, region_issues, complete_native_regions
                    try:
                        structured = parse_document(record.original_path, Path(record.original_path).parent / '.structure-cache', structured_pages)
                        structured['regions'] = complete_native_regions(record.original_path, structured['regions'], structured_pages)
                        local_pass = 'structured'
                    except ImportError:
                        record.warnings.append(_warning('structured_parser_unavailable', '未安装结构化解析组件，当前仅有原生文本，复杂版面需核验'))
                        raise
                    except Exception:
                        record.warnings.append(_warning('structured_parser_failed', '结构化解析未完成，保留原始资料和已有文本'))
                        # The accurate pass can die on a model load (observed:
                        # os error 1455 on Windows pagefile). Never let that
                        # cost the whole local pass: read every vision-routed
                        # page locally first, and only pages that still have no
                        # usable local text are worth a paid render.
                        from backend.services.structured_pdf import native_text_regions
                        lightweight = native_text_regions(record.original_path, None)
                        structured = {'regions': lightweight['regions'],
                                      'parser': 'native_light',
                                      'engine_error': lightweight.get('engine_error')}
                        if lightweight.get('engine_error'):
                            record.warnings.append(_warning(
                                'local_ocr_unavailable',
                                f"本地轻量识别引擎不可用：{lightweight['engine_error']}", severity='warning'))
                        local_pass = 'lightweight'
                    for parsed_page in record.pages:
                        parsed_page.regions = [r for r in structured['regions'] if r['page'] == parsed_page.number]
                        issues = region_issues(parsed_page.regions)
                        for issue in issues:
                            record.warnings.append(_warning('structure_needs_review', issue, parsed_page.source_location))
                        region_text = '\n'.join(r.get('text', '') for r in parsed_page.regions)
                        if not region_text.strip() or pdf_vision_mode == 'all':
                            continue
                        quality, route, _ = self._assess_pdf_page(region_text)
                        usable = len(region_text.strip()) >= _MIN_NATIVE_TEXT_CHARS and not issues
                        if not usable:
                            continue
                        parser = 'docling' if local_pass == 'structured' else 'rapidocr'
                        # Keep the recovered text as the page's text so it can
                        # be cited with an offset even when the page still goes
                        # through vision for a better reading. The paid render
                        # replaces it later with the model's own transcription.
                        parsed_page.quality['native_text'] = parsed_page.text
                        parsed_page.text = region_text
                        parsed_page.provenance.append({
                            'kind': 'structured_pdf' if local_pass == 'structured' else 'local_text_layer',
                            'parser': parser, 'location': parsed_page.source_location})
                        if any(r.get('parser') == 'pymupdf_native' for r in parsed_page.regions):
                            parsed_page.provenance.append({'kind': 'native_pdf_text_recovery', 'parser': 'pymupdf', 'location': parsed_page.source_location})
                        if parsed_page.route == 'vision':
                            parsed_page.route_reason = 'local_text_draft_pending_visual'
                        else:
                            parsed_page.quality['local_recovery_parser'] = parser
                except ImportError:
                    pass
                except Exception as exc:
                    record.warnings.append(_warning(
                        'local_text_layer_failed',
                        f'本地文字层未建立（{type(exc).__name__}），所有页面都需要远程识别', severity='warning'))
            record.extracted_text = "\n\n".join(page.text for page in record.pages)
            if not record.pages:
                record.warnings.append(_warning("pdf_empty", "PDF 未包含可解析页面"))
            if vision_adapter and record.pages:
                required_pages = {page.number for page in record.pages if page.route == "vision"}
                if required_pages:
                    selected_pages, deferred_pages = self._bounded_vision_pages(record, required_pages)
                    textless_deferred = set()
                    for number in deferred_pages:
                        page = next((p for p in record.pages if p.number == number), None)
                        if page is None:
                            continue
                        # A page the local pass already read keeps that reading
                        # and stops needing a paid render: the cap decides which
                        # pages are worth the better (paid) reading, not which
                        # pages get text at all. Pages with no local text stay
                        # queued so the operator can continue them.
                        if (page.text or '').strip():
                            page.route = 'native_text'
                            page.route_reason = 'deferred_by_page_cap'
                            # Name the pass that actually produced this text. It
                            # used to be hardcoded 'rapidocr' even when the
                            # accurate Docling pass had supplied the reading
                            # (observed: pages carrying structured_pdf/docling
                            # provenance were labelled rapidocr), which made the
                            # parameter table's provenance untrustworthy.
                            page.quality['local_recovery_parser'] = (
                                'docling' if local_pass == 'structured' else 'rapidocr')
                        else:
                            page.route_reason = 'deferred_by_page_cap'
                            textless_deferred.add(number)
                    if deferred_pages:
                        # Say what the first number actually is. ``required_pages``
                        # is the visual queue measured before the cap ran, so for a
                        # pure scan it equals the page count. The old wording
                        # ("N 页需要远程识别") read as "N pages need paid remote
                        # work", which is wrong twice over: most of those pages are
                        # then read locally and never paid for. Name the reason the
                        # page was queued instead (_assess_pdf_page: empty, too
                        # little, corrupt, or a risky layout).
                        resolved_locally = len(deferred_pages) - len(textless_deferred)
                        local_clause = (f'{resolved_locally} 页用本地文字，'
                                        if resolved_locally else '')
                        record.warnings.append(_warning(
                            'vision_page_cap_applied',
                            f'本份资料有 {len(required_pages)} 页原生文字不足采信，本次按相关度付费识别前 {len(selected_pages)} 页；'
                            f'其余 {len(deferred_pages)} 页不再付费：'
                            f'{local_clause}'
                            f'{len(textless_deferred)} 页无本地文字、待继续处理'))
                    self._vision_pdf_pages(
                        record, data, page_texts, page_has_images, vision_adapter,
                        page_numbers=selected_pages,
                    )
                    if textless_deferred and record.ocr_status == 'completed':
                        # Unread pages are neither done nor failed; calling the
                        # document fully OCR'd would hide unpaid pages.
                        record.ocr_status = 'partial'
        except ImportError:
            record.warnings.append(_warning("dependency_missing", "解析 PDF 需要安装 pypdf；原始文件哈希和路径仍已保留", severity="error"))
        except Exception as exc:
            record.warnings.append(_warning(
                "pdf_parse_failed",
                f"PDF 解析失败：{exc}；{_pdf_page_counts(record, reader, layout_pdf, pages_before)}",
                severity="error",
            ))

    def _parse_image(self, record: DocumentRecord, data: bytes,
                     vision_adapter: VisionAdapter | None = None) -> None:
        record.images = [DocumentImage(1, "image:1", record.media_type, record.sha256)]
        try:
            from PIL import Image  # type: ignore
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                record.images[0].source_location = f"image:1 ({image.width}x{image.height})"
                if vision_adapter:
                    self._vision_image(record, record.images[0], data, vision_adapter)
        except ImportError:
            record.warnings.append(_warning("dependency_missing", "解析图片真实性需要安装 Pillow；原始图片仍已保留"))
        except Exception as exc:
            record.ocr_status = "failed"
            record.warnings.append(_warning("image_decode_failed", f"图片无法解码：{exc}", "image:1", "error"))

    def _call_vision(self, adapter: VisionAdapter, data: bytes, media_type: str,
                     location: str):
        """Invoke adapters using the documented keyword contract.

        The positional fallback keeps tiny test doubles ergonomic while still
        avoiding any provider-specific dependency in the ingestion service.
        """
        try:
            return adapter.analyze(data, media_type=media_type, source_location=location)
        except TypeError as first_error:
            try:
                return adapter.analyze(data, media_type, location)  # type: ignore[misc]
            except TypeError:
                raise first_error

    @staticmethod
    def _draft_fields(fields: Any) -> dict[str, Any]:
        """Force model fields into the review state; never auto-confirm."""
        if not isinstance(fields, dict):
            return {}
        out: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, dict):
                item = dict(value)
                item["status"] = "pending_review"
                out[str(key)] = item
            else:
                out[str(key)] = {"value": value, "status": "pending_review"}
        return out

    def _refresh_ocr_status(self, record: DocumentRecord) -> None:
        statuses = [image.ocr_status for image in record.images]
        if not statuses or all(status == "not_requested" for status in statuses):
            return
        if any(status == "failed" for status in statuses):
            record.ocr_status = "failed"
        elif any(status == "pending" for status in statuses):
            record.ocr_status = "pending"
        elif any(status == "completed" for status in statuses) and all(status in {"completed", "not_requested"} for status in statuses):
            record.ocr_status = "completed"

    def _apply_vision(self, record: DocumentRecord, image: DocumentImage,
                      result: Any, *, page_number: int | None = None,
                      adapter: VisionAdapter | None = None) -> None:
        try:
            normalized = normalize_vision_result(result)
        except Exception as exc:
            image.ocr_status = "failed"
            image.failure = _vision_failure(exc, image.source_location)
            record.warnings.append(_warning("ocr_adapter_failed", f"视觉适配器结果无效：{exc}", image.source_location, "error"))
            self._refresh_ocr_status(record)
            return
        image.ocr_text = normalized.text
        image.structured_fields = self._draft_fields(normalized.fields)
        for warning in normalized.warnings:
            record.warnings.append(_warning("ocr_adapter_warning", warning, image.source_location))
        if not normalized.text.strip() and not normalized.fields:
            image.ocr_status = "failed"
            record.warnings.append(_warning("ocr_empty_result", "视觉适配器返回空文本和空字段", image.source_location, "error"))
            self._refresh_ocr_status(record)
            return
        image.ocr_status = "completed"
        if normalized.provider:
            record.model_extraction.provider = normalized.provider
        elif getattr(adapter or self.vision_adapter, "provider", None):
            record.model_extraction.provider = str((adapter or self.vision_adapter).provider)
        record.model_extraction.status = "completed"
        record.model_extraction.review_status = "pending_review"
        if normalized.text:
            record.model_extraction.text = "\n".join(x for x in (record.model_extraction.text, normalized.text) if x)
        record.model_extraction.fields.update(image.structured_fields)
        record.model_extraction.fields[image.source_location or f"image:{image.index}"] = {
            "text": normalized.text,
            "fields": image.structured_fields,
            "source_location": image.source_location,
            "status": "pending_review",
        }
        refs = normalized.source_refs or [{
            "location": image.source_location or f"image:{image.index}",
            "document_id": record.id,
            "quote": normalized.text,
        }]
        for ref in refs:
            record.model_extraction.source_refs.append({**ref, "location": ref.get("location") or image.source_location or f"image:{image.index}", "document_id": record.id})
        if normalized.text.strip():
            location = image.source_location or f"image:{image.index}"
            if page_number is not None:
                # Replace the empty source-derived page text with the OCR draft
                # while preserving its page location and document identity.
                page = next((p for p in record.pages if p.number == page_number), None)
                if page is not None and not page.text.strip():
                    page.text = normalized.text
            else:
                record.pages.append(DocumentPage(len(record.pages) + 1, normalized.text, location, record.id))
            record.extracted_text = "\n\n".join(p.text for p in record.pages if p.text.strip())
        self._refresh_ocr_status(record)

    def _vision_image(self, record: DocumentRecord, image: DocumentImage,
                      data: bytes, adapter: VisionAdapter,
                      page_number: int | None = None) -> None:
        image.ocr_status = "pending"
        if not (image.source_location or "").endswith("/rendered"):
            record.model_extraction.artifacts.append({"path": record.original_path, "media_type": image.media_type, "location": image.source_location or f"image:{image.index}"})
        try:
            from backend.services.vision_regions import analyze_regions
            from backend.services.analysis_checkpoint import current_checkpoint
            checkpoint = current_checkpoint.get()
            def call_region(raw, mime, loc):
                if checkpoint is None:
                    return self._call_vision(adapter, raw, mime, loc)
                from backend.services.vision import VisionAdapterError
                from dataclasses import asdict
                checkpoint.check()
                key = checkpoint.key(adapter, {'document': record.id, 'sha256': _sha256(raw), 'location': loc, 'media_type': mime})
                cached = checkpoint.get(key)
                if cached and cached['status'] == 'completed':
                    return cached['result']
                if cached and cached.get('code') == 'vision_output_truncated':
                    raise VisionAdapterError('vision_output_truncated', '继续已拆分的视觉区域')
                scope = {'stage': 'vision', 'documents': [record.id], 'location': loc}
                checkpoint.save(key, 'running', scope=scope)
                try:
                    result = self._call_vision(adapter, raw, mime, loc)
                except Exception as exc:
                    checkpoint.save(key, 'failed', code=getattr(exc, 'code', 'vision_failed'), scope=scope)
                    raise
                checkpoint.save(key, 'completed', result=asdict(normalize_vision_result(result)), scope=scope)
                checkpoint.check()
                return result
            result = analyze_regions(call_region, data,
                                     image.media_type or "application/octet-stream", image.source_location or f"image:{image.index}")
            self._apply_vision(record, image, result, page_number=page_number, adapter=adapter)
        except Exception as exc:
            image.ocr_status = "failed"
            image.failure = _vision_failure(exc, image.source_location)
            record.model_extraction.status = "failed"
            record.warnings.append(_warning("ocr_adapter_failed", f"视觉适配器调用失败：{exc}", image.source_location, "error"))
            self._refresh_ocr_status(record)

    def _vision_pdf_pages(self, record: DocumentRecord, data: bytes,
                          page_texts: list[str], page_has_images: list[bool],
                          adapter: VisionAdapter,
                          page_numbers: set[int] | None = None) -> None:
        """Render exactly the pages given; choosing and capping pages is the
        caller's job (``_parse_pdf`` for automatic ingestion, the API for an
        explicit retry, which must never be silently narrowed)."""
        try:
            import fitz  # type: ignore
            pdf = fitz.open(stream=data, filetype="pdf")
        except ImportError:
            record.warnings.append(_warning("dependency_missing", "扫描 PDF 视觉解析需要安装 PyMuPDF；原始文件和文本页仍已保留"))
            record.model_extraction.status = "failed"
            return
        except Exception as exc:
            record.warnings.append(_warning("pdf_render_failed", f"PDF 页面渲染失败：{exc}", severity="error"))
            record.model_extraction.status = "failed"
            return
        try:
            page_numbers = set(page_numbers or [])
            for page_number in sorted(page_numbers):
                page_obj = next((item for item in record.pages if item.number == page_number), None)
                if page_obj is None:
                    continue
                text = page_obj.text
                idx = page_number
                page_obj.route = "vision"
                # Keep the locally recovered reading next to the paid one: it is
                # the audit trail for what the local pass saw, and it is the only
                # text left on pages the cap defers.
                if (page_obj.text or '').strip():
                    page_obj.quality['local_text_draft'] = page_obj.text
                if page_obj.route_reason in {None, "native_text_reliable", "local_text_recovery", "local_text_draft_pending_visual"}:
                    page_obj.route_reason = "explicit_page_retry"
                page = pdf.load_page(idx - 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                png = pix.tobytes("png")
                location = f"page:{idx}/rendered"
                record.images = [image for image in record.images if image.source_location != location]
                record.model_extraction.fields.pop(location, None)
                record.model_extraction.source_refs = [
                    ref for ref in record.model_extraction.source_refs
                    if ref.get("location") != location
                ]
                record.model_extraction.artifacts = [
                    artifact for artifact in record.model_extraction.artifacts
                    if artifact.get("location") != location
                ]
                record.warnings = [warning for warning in record.warnings if warning.location != location]
                image = DocumentImage(len(record.images) + 1, location, "image/png", _sha256(png), "pending")
                record.images.append(image)
                artifact_path = self.storage_dir / "visual_inputs" / record.id / f"page-{idx}.png"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(png)
                record.model_extraction.artifacts.append({"path": str(artifact_path), "media_type": "image/png", "location": location, "page": idx})
                self._vision_image(record, image, png, adapter, page_number=idx)
                if page_obj is not None:
                    page_obj.provenance.append({
                        "kind": "visual_page_analysis",
                        "location": location,
                        "status": image.ocr_status,
                        "provider": (
                            record.model_extraction.provider
                            or getattr(adapter, "provider", type(adapter).__name__)
                        ),
                        "sha256": image.sha256,
                    })
                if image.ocr_status == "completed" and not text.strip():
                    if page_obj is not None and image.ocr_text:
                        page_obj.text = image.ocr_text
                from backend.services.analysis_checkpoint import current_checkpoint
                checkpoint = current_checkpoint.get()
                if checkpoint:
                    checkpoint.repo.put('documents', record.id, record.to_dict(), task_id=checkpoint.task_id)
                    checkpoint.check()
            record.extracted_text = "\n\n".join(p.text for p in record.pages if p.text.strip())
            required_pages = self.required_vision_pages(record)
            rendered = [
                image for image in record.images
                if (
                    (match := re.fullmatch(
                        r"page:(\d+)/rendered", str(image.source_location or "")
                    ))
                    and int(match.group(1)) in required_pages
                )
            ]
            if any(image.ocr_status == "failed" for image in rendered):
                record.model_extraction.status = "failed"
                record.ocr_status = "failed"
            elif rendered and all(image.ocr_status == "completed" for image in rendered):
                record.model_extraction.status = "completed"
                record.ocr_status = "completed"
        finally:
            pdf.close()

    def analyze_pdf_pages(self, record: DocumentRecord, page_numbers: set[int],
                          adapter: VisionAdapter) -> DocumentRecord:
        """Retry selected rendered pages while preserving completed page evidence."""
        if record.document_type != "pdf" or not record.original_path:
            raise ValueError("record must be a persisted PDF")
        data = Path(record.original_path).read_bytes()
        record.pages = [
            page for page in record.pages
            if not (page.source_location or "").endswith("/rendered")
        ]
        page_texts = [page.text for page in record.pages]
        self._vision_pdf_pages(
            record, data, page_texts, [True] * len(page_texts), adapter,
            page_numbers=set(page_numbers),
        )
        return record


__all__ = ["DocumentIngestionService"]
