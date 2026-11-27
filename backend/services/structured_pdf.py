"""Optional Docling adapter preserving regions, with explicit availability."""
from __future__ import annotations
import hashlib
import json
import os
import re
import signal
from pathlib import Path
from functools import lru_cache
import threading
import subprocess
import sys
import time
from datetime import datetime, timezone

_PARSE_LOCK = threading.RLock()
_OPERATION_STATE = threading.local()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PROCS: dict[str, dict[int, subprocess.Popen]] = {}

#: How much text a local pass must recover before a page can skip paid vision.
MIN_NATIVE_TEXT_CHARS = 32

#: Rendered resolution of the lightweight pass. Lower than the paid renderer's
#: 1.5 because the paid path sends the image over the network while this one
#: only has to be legible for onnxruntime on the same machine.
LIGHT_ZOOM = 1.2

#: Environment override for the RapidOCR (PP-OCR) onnxruntime *intra-op* thread
#: count used by the accurate Docling pass. Reading this is the only knob: the
#: layout model's thread count stays pinned to 1 (see ``layout_num_threads``),
#: because RT-DETR multi-thread inference has raised native access violations on
#: Windows (设计规范). Setting ``LAB_PDF_OCR_THREADS=1`` restores the previous
#: single-threaded behaviour without a code change.
OCR_THREADS_ENV = 'LAB_PDF_OCR_THREADS'
#: Default when the environment variable is absent. A scanned A4 page at the
#: Docling default scale (3.0) spends almost all of its OCR time inside
#: onnxruntime, and one intra-op thread leaves every other core idle. Four is
#: deliberately below the core count: eight threads were measured to give the
#: same reading, but sit closer to the pagefile pressure already recorded on
#: this 16GB machine (设计规范, ``OSError 1455``).
DEFAULT_OCR_THREADS = 4
_OCR_THREADS_MIN = 1
_OCR_THREADS_MAX = 16


def ocr_threads() -> int:
    """RapidOCR intra-op thread count, from the environment, clamped and safe.

    A malformed or out-of-range value must never take the local pass down: the
    fallback is the conservative default rather than an exception.
    """
    raw = os.environ.get(OCR_THREADS_ENV, '').strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_OCR_THREADS
    else:
        value = DEFAULT_OCR_THREADS
    return max(_OCR_THREADS_MIN, min(_OCR_THREADS_MAX, value))


def layout_num_threads() -> int:
    """RT-DETR layout thread count. Fixed at 1 on Windows by design."""
    return 1


@lru_cache(maxsize=1)
def _native_ocr_engine():
    """RapidOCR for the lightweight pass, sharing the OCR thread knob.

    Built lazily and cached: the engine owns onnxruntime sessions, so it is
    created once per process and never re-entered concurrently.
    """
    from rapidocr import RapidOCR
    return RapidOCR(params={'EngineConfig.onnxruntime.intra_op_num_threads': ocr_threads()})


def _as_sequence(value):
    """Normalise an OCR result field to a list without using truthiness.

    RapidOCR returns ``txts``/``scores`` as tuples and ``boxes`` as a numpy
    array. ``value or []`` is not a safe default for those: a multi-element
    numpy array raises ``ValueError`` on ``bool()``. Doing the check by
    identity keeps the lightweight pass working for every engine shape.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return []


@lru_cache(maxsize=1)
def _converter():
    os.environ.setdefault('HF_HOME', str(Path('data/model-cache').resolve()))
    from docling.document_converter import DocumentConverter
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import PdfFormatOption
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorOptions

    options = PdfPipelineOptions()
    if os.name == 'nt':
        # Windows CPU RT-DETR pooling has raised native access violations in
        # multi-thread inference. Keep the same models, OCR and resolution.
        options.accelerator_options = AcceleratorOptions(num_threads=layout_num_threads())
        options.layout_batch_size = 1
    options.do_ocr = True
    # The accelerator option above also configures RapidOCR's onnxruntime
    # session, which is why the whole local pass used to run single-threaded on
    # OCR as well. ``rapidocr_params`` is forwarded by docling to
    # ``RapidOCR(params=...)`` after its own defaults, so this overrides only
    # the OCR engine's intra-op parallelism and leaves the layout model, the
    # rendered scale and the detected boxes untouched.
    options.ocr_options = RapidOcrOptions(
        lang=["ch"], backend="onnxruntime",
        rapidocr_params={'EngineConfig.onnxruntime.intra_op_num_threads': ocr_threads()})
    options.do_table_structure = True
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options, backend=PyPdfiumDocumentBackend)})
    return converter


def native_text_regions(path, page_numbers=None):
    """Recover a text layer locally, without Docling's layout/table models.

    Why this exists
    ---------------
    ``parse_document`` is the accurate path, but it loads Docling's TableFormer
    and layout models. On a memory-constrained machine that load can fail
    outright (observed: ``OSError: 页面文件太小 (os error 1455)`` raising out of
    the table model), and because the whole converter is one unit, a single
    model-load failure used to cost the *entire* local pass. Every page then
    fell back to a paid remote vision call, so a 17-page scan took ~15 minutes
    of serial API calls and a 1000-page manual would never finish.

    This pass answers the cheaper question first: "can we read this page
    locally at all?" It uses PyMuPDF's native text when present and RapidOCR
    (onnxruntime, ``ch_ppocr_mobile``/``v6`` recognition) otherwise, emitting the
    same region shape as ``parse_document``. It deliberately does **not** load
    table-structure models, so it never competes for that memory and never
    turns a table-model failure into "no text at all". Callers use it to route
    pages: pages with enough local text skip the paid call, and the remaining
    pages are ranked before any of them is rendered remotely.

    Measured on the pure-scan acceptance sample: 0.8s engine init, ~1.5s per
    page, 380 characters recovered from a page whose native text layer is empty.
    """
    started, clock = time.time(), time.perf_counter()
    outcome = 'completed'
    try:
        import fitz

        selected = sorted(set(page_numbers or []))
        regions = []
        engine = None
        engine_error = None
        with fitz.open(path) as pdf:
            for number, page in enumerate(pdf, 1):
                if selected and number not in selected:
                    continue
                native_text = page.get_text().strip()
                engine_name = 'pymupdf_native'
                if len(native_text) >= MIN_NATIVE_TEXT_CHARS:
                    regions.append({
                        'id': f'native-light:{number}:0', 'page': number,
                        'bbox': {'l': 0.0, 't': 0.0, 'r': float(page.rect.width), 'b': float(page.rect.height),
                                 'coord_origin': 'TOPLEFT'},
                        'label': 'text', 'level': 1, 'text': native_text, 'cells': [],
                        'parser': engine_name, 'location': f'page:{number}/native-light:0',
                    })
                    continue
                if engine is None and engine_error is None:
                    try:
                        engine = _native_ocr_engine()
                    except Exception as exc:  # noqa: BLE001 - report, never guess
                        engine_error = f'{type(exc).__name__}: {exc}'
                if engine is None:
                    continue
                try:
                    import numpy as np
                    pix = page.get_pixmap(matrix=fitz.Matrix(LIGHT_ZOOM, LIGHT_ZOOM), alpha=False)
                    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                    result = engine(image)
                except Exception as exc:  # noqa: BLE001
                    regions.append({'id': f'native-light-failed:{number}', 'page': number, 'bbox': None,
                                    'label': 'ocr_failed', 'level': 1, 'text': '', 'cells': [],
                                    'parser': 'rapidocr',
                                    'location': f'page:{number}/native-light:failed',
                                    'recovery_warning': f'本地轻量识别未完成（{type(exc).__name__}）'})
                    continue
                # ``boxes`` is a numpy array and ``txts``/``scores`` are tuples.
                # ``x or []`` on an array raises ValueError ("truth value of an
                # array with more than one element is ambiguous"), which used to
                # escape this function and be swallowed by the caller as
                # ``local_text_layer_failed`` -- so every page silently went to paid
                # vision and ``local_recovery_parser`` stayed empty. Normalise by
                # explicit None check, never by truthiness.
                texts = _as_sequence(getattr(result, 'txts', None))
                boxes = _as_sequence(getattr(result, 'boxes', None))
                scores = _as_sequence(getattr(result, 'scores', None))
                for index, text in enumerate(texts):
                    if not str(text).strip():
                        continue
                    bbox = boxes[index] if index < len(boxes) else None
                    regions.append({
                        'id': f'native-light:{number}:{index}', 'page': number,
                        'bbox': {'l': float(bbox[0][0]), 't': float(bbox[0][1]),
                                 'r': float(bbox[2][0]), 'b': float(bbox[2][1]), 'coord_origin': 'TOPLEFT'} if bbox is not None else None,
                        'label': 'text', 'level': 1, 'text': str(text), 'cells': [],
                        'parser': 'rapidocr',
                        'confidence': float(scores[index]) if index < len(scores) else None,
                        'location': f'page:{number}/native-light:{index}',
                    })
        return {'parser': 'native_light', 'regions': regions,
                'engine_error': engine_error}
    except Exception:
        outcome = 'failed'
        raise
    finally:
        _OPERATION_STATE.entry = _operation_timing(
            'native_light', started, clock, outcome,
            requested_pages=len(selected) if 'selected' in locals() else 0,
            all_pages=not selected if 'selected' in locals() else False,
            pages=len({region['page'] for region in regions}) if 'regions' in locals() else 0)


def _operation_timing(operation, started, clock, outcome, **metadata):
    return {
        'operation': operation, 'status': outcome,
        'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(),
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'duration_ms': max(0, int(round((time.perf_counter() - clock) * 1000))),
        'round': 1, **metadata,
    }


def _register_active_proc(owner_id: str | None, proc: subprocess.Popen) -> str:
    key = str(owner_id or '_global')
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.setdefault(key, {})[id(proc)] = proc
    return key


def _unregister_active_proc(key: str, proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        bucket = _ACTIVE_PROCS.get(key)
        if not bucket:
            return
        bucket.pop(id(proc), None)
        if not bucket:
            _ACTIVE_PROCS.pop(key, None)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate *proc* and any child processes spawned by Docling/OCR."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    pid = getattr(proc, 'pid', None)
    if os.name == 'nt' and isinstance(pid, int) and pid > 0:
        try:
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    elif isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:
        pass


def terminate_active_subprocesses(owner_id: str | None = None) -> int:
    """Force-stop active Docling worker subprocesses for *owner_id* (or all)."""
    with _ACTIVE_LOCK:
        if owner_id is None:
            targets = [proc for bucket in _ACTIVE_PROCS.values() for proc in bucket.values()]
        else:
            targets = list((_ACTIVE_PROCS.get(str(owner_id)) or {}).values())
    killed = 0
    for proc in targets:
        try:
            if proc.poll() is None:
                _kill_process_tree(proc)
                killed += 1
        except Exception:
            pass
    return killed


def extract_attachment_pdf_text(
    path,
    cache_dir,
    *,
    max_pages: int = 10,
    timeout: float = 45.0,
    cancel_check=None,
) -> dict:
    """Lightweight bounded PDF extraction for research attachments (no Docling).

    Reads up to *max_pages* pages using PyMuPDF native text first, falling back
    to RapidOCR only on image-heavy pages within the remaining *timeout* budget,
    and caches complete/max-page-bounded outputs by SHA-256 across rounds.
    """
    started, clock = time.time(), time.perf_counter()
    page_cap = max(1, int(max_pages))
    budget_s = max(1.0, float(timeout))
    raw_bytes = Path(path).read_bytes()
    key = hashlib.sha256(raw_bytes).hexdigest()
    cache = Path(cache_dir) / f'{key}-light-v1-p{page_cap}.json'
    if cache.exists():
        cached = json.loads(cache.read_text(encoding='utf-8'))
        cached['cache'] = 'hit'
        cached['duration_ms'] = 0
        return cached

    import fitz

    warnings: list[dict] = []
    page_texts: dict[int, str] = {}
    ocr_needed: list[int] = []
    truncated = False
    truncated_reason = ''
    used_native = False
    used_ocr = False
    total_pages = 0

    def _cancelled() -> bool:
        if not callable(cancel_check):
            return False
        try:
            return bool(cancel_check())
        except Exception:
            return False

    try:
        with fitz.open(path) as pdf:
            total_pages = len(pdf)
            limit = min(total_pages, page_cap)
            if total_pages > page_cap:
                truncated = True
                truncated_reason = 'max_pages'
            for page_no in range(1, limit + 1):
                if _cancelled():
                    raise RuntimeError('本地PDF解析已取消')
                if time.perf_counter() - clock >= budget_s:
                    truncated = True
                    truncated_reason = 'timeout'
                    break
                page = pdf[page_no - 1]
                native_text = page.get_text().strip()
                if len(native_text) >= MIN_NATIVE_TEXT_CHARS:
                    page_texts[page_no] = native_text
                    used_native = True
                else:
                    ocr_needed.append(page_no)

            if ocr_needed and truncated_reason != 'timeout':
                engine = None
                try:
                    engine = _native_ocr_engine()
                except Exception as exc:  # noqa: BLE001
                    warnings.append({
                        'code': 'local_ocr_unavailable',
                        'severity': 'error',
                        'message': f'本地轻量识别引擎不可用：{type(exc).__name__}: {exc}',
                    })
                if engine is not None:
                    import numpy as np
                    for page_no in ocr_needed:
                        if _cancelled():
                            raise RuntimeError('本地PDF解析已取消')
                        if time.perf_counter() - clock >= budget_s:
                            truncated = True
                            truncated_reason = 'timeout'
                            break
                        page = pdf[page_no - 1]
                        try:
                            pix = page.get_pixmap(matrix=fitz.Matrix(LIGHT_ZOOM, LIGHT_ZOOM), alpha=False)
                            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                            result = engine(image)
                            texts = [str(t).strip() for t in _as_sequence(getattr(result, 'txts', None)) if str(t).strip()]
                            if texts:
                                page_texts[page_no] = '\n'.join(texts)
                                used_ocr = True
                        except Exception as exc:  # noqa: BLE001
                            warnings.append({
                                'code': 'local_ocr_page_failed',
                                'severity': 'warning',
                                'message': f'第 {page_no} 页轻量识别失败（{type(exc).__name__}）',
                            })
    except Exception as exc:  # noqa: BLE001
        if _cancelled() or '已取消' in str(exc):
            raise
        warnings.append({
            'code': 'pdf_unreadable',
            'severity': 'error',
            'message': f'PDF文件损坏或无法读取（{type(exc).__name__}）',
        })
        truncated_reason = truncated_reason or 'error'

    ordered_pages = [page_texts[n] for n in sorted(page_texts) if page_texts[n].strip()]
    processed_pages = len(page_texts)
    if truncated_reason == 'max_pages':
        warnings.append({
            'code': 'attachment_pdf_truncated',
            'severity': 'info',
            'message': f'外部 PDF 共 {total_pages} 页，已轻量提取前 {min(total_pages, page_cap)} 页',
        })
    elif truncated_reason == 'timeout':
        warnings.append({
            'code': 'attachment_pdf_timeout',
            'severity': 'warning',
            'message': f'外部 PDF 解析达到 {int(budget_s)} 秒上限，已保留前 {processed_pages} 页可读内容',
        })

    parser_name = 'rapidocr_light' if used_ocr and not used_native else ('pymupdf+rapidocr_light' if used_ocr else 'pymupdf_light')
    duration_ms = max(0, int(round((time.perf_counter() - clock) * 1000)))
    output = {
        'text': '\n\n'.join(ordered_pages),
        'total_pages': total_pages,
        'processed_pages': processed_pages,
        'truncated': truncated,
        'truncated_reason': truncated_reason,
        'parser': parser_name,
        'warnings': warnings,
        'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(),
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'duration_ms': duration_ms,
        'cache': 'miss',
    }
    if truncated_reason not in {'timeout', 'error'}:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(cache.name + f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(output, ensure_ascii=False), encoding='utf-8')
        temporary.replace(cache)
    return output


def parse_document(path, cache_dir, page_numbers=None, *, timeout=600, cancel_check=None, owner_id=None):
    """Contain native PDF/OCR faults in a fresh process; reuse valid caches."""
    started, clock = time.time(), time.perf_counter()
    key = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    selected = sorted(set(page_numbers or []))
    suffix = hashlib.sha256(str(selected).encode()).hexdigest()[:12] if selected else 'all'
    cache = Path(cache_dir) / (key + '-docling-v7-' + suffix + '.json')
    outcome = 'completed'
    cache_status = 'miss'
    try:
        with _PARSE_LOCK:
            if cache.exists():
                cache_status = 'hit'
                return json.loads(cache.read_text(encoding='utf-8'))
            cache.parent.mkdir(parents=True, exist_ok=True)
            diagnostic = cache.with_suffix('.worker.log')
            command = [sys.executable, '-X', 'utf8', '-m', 'backend.services.structured_pdf',
                       str(Path(path).resolve()), str(Path(cache_dir).resolve()), json.dumps(selected)]
            flags = (subprocess.CREATE_NO_WINDOW | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)) if os.name == 'nt' else 0
            with diagnostic.open('w', encoding='utf-8') as log:
                proc = subprocess.Popen(
                    command,
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=log,
                    stderr=log,
                    creationflags=flags,
                    start_new_session=(os.name != 'nt'),
                )
                reg_key = _register_active_proc(owner_id, proc)
                wait_clock = time.perf_counter()
                returncode = None
                try:
                    while True:
                        if callable(cancel_check) and cancel_check():
                            _kill_process_tree(proc)
                            outcome = 'stopped'
                            raise RuntimeError('本地结构解析已取消，原件和已完成节点保留')
                        returncode = proc.poll()
                        if returncode is not None:
                            break
                        if time.perf_counter() - wait_clock >= float(timeout):
                            _kill_process_tree(proc)
                            outcome = 'stopped'
                            raise RuntimeError('本地结构解析超时，原件和已完成节点保留，可继续恢复')
                        time.sleep(0.25)
                finally:
                    _unregister_active_proc(reg_key, proc)
            if returncode or not cache.exists():
                outcome = 'failed'
                raise RuntimeError('本地结构解析子进程失败，原件保留；未把失败当作解析成功')
            return json.loads(cache.read_text(encoding='utf-8'))
    except Exception:
        if outcome == 'completed':
            outcome = 'failed'
        raise
    finally:
        _OPERATION_STATE.entry = _operation_timing(
            'docling', started, clock, outcome, cache=cache_status,
            requested_pages=len(selected), all_pages=not selected)


def _parse_in_process(path, cache_dir, page_numbers=None):
    key = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    selected = sorted(set(page_numbers or []))
    suffix = hashlib.sha256(str(selected).encode()).hexdigest()[:12] if selected else 'all'
    cache = Path(cache_dir) / (key + '-docling-v7-' + suffix + '.json')
    with _PARSE_LOCK:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        ranges = []
        for number in selected:
            if ranges and number == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], number)
            else:
                ranges.append((number, number))
        documents = [_converter().convert(path, **({'page_range': bounds} if bounds else {})).document for bounds in (ranges or [None])]
    regions = []
    for document in documents:
        for index, (item, level) in enumerate(document.iterate_items()):
            text = getattr(item, 'text', '')
            if str(getattr(item, 'label', '')) == 'picture':
                continue
            if hasattr(item, 'export_to_markdown'):
                text = item.export_to_markdown(doc=document)
            cells = []
            data = getattr(item, 'data', None)
            for cell in getattr(data, 'table_cells', []):
                cells.append({'row_start': cell.start_row_offset_idx, 'row_end': cell.end_row_offset_idx,
                              'col_start': cell.start_col_offset_idx, 'col_end': cell.end_col_offset_idx,
                              'text': cell.text, 'bbox': cell.bbox.model_dump() if cell.bbox else None})
            for prov in getattr(item, 'prov', []):
                regions.append({'id': f'region:{prov.page_no}:{index}', 'page': prov.page_no, 'bbox': prov.bbox.model_dump(),
                                'label': str(getattr(item, 'label', 'text')), 'level': level, 'text': text, 'cells': cells,
                                'location': f'page:{prov.page_no}/region:{prov.page_no}:{index}'})
    # Layout detection can classify a native model caption as part of a
    # picture. Restore omitted native blocks with coordinates, not OCR guesses.
    import fitz
    with fitz.open(path) as native:
        for number, page in enumerate(native, 1):
            if selected and number not in selected:
                continue
            # Native ruled tables are often more precise than OCR for sparse
            # borders. Use geometry and real text, never filename/model rules.
            from backend.services.document_ingestion import _geometric_cells
            try:
                native_tables = page.find_tables().tables if len(page.get_text().strip()) > 100 else []
            except Exception:
                native_tables = []
            for table_index, table in enumerate(native_tables):
                rows = [[str(c or '') for c in row] for row in table.extract()]
                cells = _geometric_cells(table, rows)
                if len(rows) < 3 or table.col_count < 2 or not cells:
                    continue
                box = fitz.Rect(table.bbox)
                if sum(len(c['text']) for c in cells) < 50:
                    continue
                for region in regions:
                    if region['page'] != number or 'table' not in region['label']:
                        continue
                    b = region['bbox']
                    rb = fitz.Rect(b['l'], page.rect.height-b['t'], b['r'], page.rect.height-b['b']) if b.get('coord_origin') == 'BOTTOMLEFT' else fitz.Rect(b['l'], b['t'], b['r'], b['b'])
                    if (rb & box).get_area() / max(box.get_area(), 1) < .8:
                        continue
                    region.update(text='\n'.join('| ' + ' | '.join(row) + ' |' for row in rows), cells=cells, parser='native_ruled_table')
                    break
            existing = ''.join(r['text'] for r in regions if r['page'] == number)
            for region in regions:
                if region['page'] == number and 'table' in region['label'] and region.get('parser') != 'native_ruled_table':
                    try:
                        from backend.services.raster_tables import recover_table
                        with _PARSE_LOCK:
                            recovered = recover_table(page, region)
                        if recovered:
                            region.update(recovered)
                    except (ImportError, ValueError, RuntimeError):
                        region['recovery_warning'] = '本地表格单元格复核未完成，保留原结构待核验'
            norm = lambda value: re.sub(r'\s+', '', value)
            for index, block in enumerate(page.get_text('blocks')):
                text = block[4].strip()
                if len(text) < 4 or norm(text) in norm(existing):
                    continue
                if not re.search(r'型号|model|type\s*:', text, re.I):
                    continue
                region = {'id': f'native:{number}:{index}', 'page': number,
                          'bbox': {'l': block[0], 't': block[1], 'r': block[2], 'b': block[3], 'coord_origin': 'TOPLEFT'},
                          'label': 'model_caption', 'level': 1, 'text': text,
                          'location': f'page:{number}/native:{index}'}
                insert = next((i for i,r in enumerate(regions) if r['page'] > number or (r['page'] == number and (page.rect.height - r['bbox']['t'] if r['bbox'].get('coord_origin') == 'BOTTOMLEFT' else r['bbox']['t']) > block[1])),len(regions))
                regions.insert(insert, region)
    result = {'parser': 'docling', 'regions': regions}
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(cache.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(cache)
    return result


def complete_native_regions(path, regions, page_numbers):
    """Restore native annotations omitted when layout detection labels a picture.

    Keep coordinates and provenance. Never append a flattened native text block
    over an existing table: that would destroy its row/column ownership.
    Also applies to cached Docling results without rerunning its models.
    """
    import fitz
    import unicodedata
    completed = list(regions)
    normalize = lambda text: re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    with fitz.open(path) as pdf:
        for number in page_numbers:
            page = pdf[number - 1]
            existing = normalize(' '.join(r.get('text', '') for r in completed if r['page'] == number))
            table_boxes = []
            for region in completed:
                b = region.get('bbox')
                if region['page'] != number or 'table' not in region.get('label', '') or not b:
                    continue
                table_boxes.append(fitz.Rect(b['l'], page.rect.height-b['t'], b['r'], page.rect.height-b['b'])
                                   if b.get('coord_origin') == 'BOTTOMLEFT' else fitz.Rect(b['l'], b['t'], b['r'], b['b']))
            for index, block in enumerate(page.get_text('blocks')):
                text = block[4].strip()
                if len(text) < 4 or normalize(text) in existing:
                    continue
                box = fitz.Rect(block[:4])
                if any((box & table).get_area() / max(box.get_area(), 1) > .5 for table in table_boxes):
                    continue
                recovered = {'id': f'native-text:{number}:{index}', 'page': number,
                                  'bbox': {'l': box.x0, 't': box.y0, 'r': box.x1, 'b': box.y1, 'coord_origin': 'TOPLEFT'},
                                  'label': 'text', 'level': 1, 'text': text, 'cells': [],
                                  'parser': 'pymupdf_native', 'location': f'page:{number}/native-text:{index}'}
                def later(region):
                    b = region.get('bbox')
                    if region['page'] != number:
                        return region['page'] > number
                    if not b:
                        return False
                    top = page.rect.height - b['t'] if b.get('coord_origin') == 'BOTTOMLEFT' else b['t']
                    return top > box.y0
                insert = next((i for i, region in enumerate(completed) if later(region)), len(completed))
                completed.insert(insert, recovered)
                existing += normalize(text)
    return completed


def region_issues(regions):
    issues = []
    for region in regions:
        if region.get('recovery_warning'):
            issues.append(region['recovery_warning'])
        if 'table' not in region.get('label', ''):
            continue
        rows = [[c.strip() for c in line.strip().strip('|').split('|')] for line in region.get('text', '').splitlines() if '|' in line and not re.fullmatch(r'[| :\-]+', line)]
        if len(rows) >= 4:
            labels = [row[0] for row in rows[1:] if row]
            numeric = sum(bool(re.match(r'^[<>≤≥±]?\s*\d', label)) for label in labels)
            if numeric / max(len(labels), 1) > .65:
                issues.append('表格行名称可能缺失，需核对原页后提取参数')
    return issues


if __name__ == '__main__':
    _parse_in_process(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
