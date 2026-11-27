"""Conservative model-specific evidence scopes; no file-specific exceptions."""
import copy
import re
from backend.services.procurement_rules import contains_model


def expand_cells(rows, cells):
    """Only a recorded cell rectangle may share a value across columns."""
    result = copy.deepcopy(rows)
    for cell in cells or []:
        r0, r1, c0, c1 = (cell.get(k, -1) for k in ('row_start', 'row_end', 'col_start', 'col_end'))
        if not all(isinstance(v, int) for v in (r0, r1, c0, c1)) or not (0 <= r0 < r1 <= len(rows)):
            continue
        for r in range(r0, r1):
            if not 0 <= c0 < c1 <= len(rows[r]):
                continue
            for c in range(c0, c1):
                if not result[r][c]:
                    result[r][c] = str(cell.get('text') or '')
    return result


def project_table(rows, model, other_models, cells=None):
    """Keep row labels and only columns owned by the chosen model."""
    rows = [[str(c or '') for c in row] for row in rows]
    rows = expand_cells(rows, cells)
    rows = [row for row in rows if not re.search(r'配件|选配|订货号|accessor|optional', ' '.join(row[:1]), re.I)]
    header = next((row for row in rows if any(contains_model(c, model) for c in row)), None)
    if header is None:
        return []
    model_columns = [i for i, cell in enumerate(header) if contains_model(cell, model)]
    foreign_columns = [i for i, cell in enumerate(header) if any(contains_model(cell, m) for m in other_models)]
    if set(model_columns) & set(foreign_columns):
        return []  # merged ambiguous cells require review, not inheritance
    if not foreign_columns:
        # Vertical product table: exclude rows naming another model.
        selected, active = [], False
        for row in rows:
            own = any(contains_model(c, model) for c in row)
            foreign = any(contains_model(' '.join(row), m) for m in other_models)
            if own or foreign:
                active = own and not foreign
            if active:
                selected.append(row)
        return selected
    first_model = min(model_columns + foreign_columns)
    columns = list(range(first_model)) + model_columns
    # A compacted merged row has lost its column positions. Do not shift values.
    return [[row[i] for i in columns] for row in rows if len(row) == len(header)]


def project_markdown(text, model, other_models, cells=None):
    lines = [line for line in text.splitlines() if '|' in line]
    if len(lines) < 2:
        return ''
    rows = [[c.strip() for c in line.strip().strip('|').split('|')] for line in lines if not re.fullmatch(r'[| :\-]+', line)]
    if cells:
        height = max(c['row_end'] for c in cells)
        width = max(c['col_end'] for c in cells)
        if height <= 1000 and width <= 100:
            rows = expand_cells([[''] * width for _ in range(height)], cells)
    return '\n'.join(' | '.join(row) for row in project_table(rows, model, other_models))


#: 参数/规格页的标题，以及参数表里必然出现的量纲。用来把"内容是参数、只是没重复
#: 型号"的页认出来（见 ``_specification_fallback``）。
_SPECIFICATION_HEADING = re.compile(
    r'技术参数|技术数据|技术规格|规格参数|主要参数|性能参数|产品参数|技术指标|参数表'
    r'|specifications?|technical\s+(data|specifications?)|datasheet', re.I)
_SPECIFICATION_UNIT = re.compile(
    r'(℃|°C|MPa|kPa|bar|L/min|l/min|m3/h|kW|\bW\b|\bV\b|\bHz\b|\bmm\b|\bcm\b|\bkg\b|\bg\b'
    r'|\bdB\b|\brpm\b|\bVAC\b|\bmAh\b|%)')


def _looks_like_specification(text: str) -> bool:
    if not text:
        return False
    if _SPECIFICATION_HEADING.search(text):
        return True
    return len(_SPECIFICATION_UNIT.findall(text)) >= 5


def _specification_fallback(text: str, other_models: list,
                            structured_model_anchor: bool = False) -> str:
    """没有型号归属、但内容是参数/规格时整页保留；否则返回空串。

    为什么需要这条：参数表天然不重复型号串。2026-10-03 批2 的 IKA 手册第 28 页
    （"操作电压 VAC 230 / 最大输入功率 W 1800 / 工作温度范围 °C -30 ~ +100"）
    通篇没有 ``HRC 2 control``，按型号白名单过滤会把整页连参数一起删掉——模型随后
    只好从封面编号和零部件清单里凑出"订货号""管接头NW"当参数。

    为什么不能干脆取消过滤：同一份资料里的**无关页**同样没有型号串，取消过滤会让
    "不属于这个候选的相邻页"变成它的证据（``tests/test_deepseek_analysis.py`` 的
    ``test_visual_candidate_evidence_excludes_unowned_adjacent_pages`` 钉的就是这条）。
    所以豁免只给"看得出来的参数内容"，并且明确写了别的型号的页不给。

    ``structured_model_anchor`` 是第三种情况（2026-10-03 批2 的水槽：2 页 -> 0 页）：
    型号只存在于**表格单元格**里，文字层被按列宽切碎（``SSW- W-4 -420- 0-2 -2S``），
    而资料又把两种规格写成一个合体型号（``SSW-420/600-2S``）铺在每一页。于是
    "这一页里出现了别的型号"照旧成立，参数页却两页都不属于任何候选。这一位为真时
    不再看"别的型号"——多规格合册里两种规格本来就共处一页，该由表格列投影去切。
    """
    if not _looks_like_specification(text):
        return ''
    if not structured_model_anchor and any(contains_model(text, other) for other in other_models):
        return ''
    return text


def _table_cells_have_model(doc, model: str) -> bool:
    """表格单元格里能不能认出这个型号（结构化数据，比被打碎的文字层可信）。"""
    for table in doc.get('tables') or []:
        for row in table.get('rows') or []:
            if contains_model(' '.join(str(cell or '') for cell in row), model):
                return True
    return False


def scoped_documents(candidate, documents, inventory):
    """Bound detail retrieval to owned text, including multi-model table headers.

    A continuation without a model anchor remains unknown rather than being
    borrowed from an unrelated instrument. Structured regions can retain a
    heading and its following blocks without including adjacent products.

    A page that carries specification content without repeating the model token
    is kept (see ``_specification_fallback``); every other page still needs a
    model anchor.
    """
    model = candidate.get('model', '')
    other_models = [x.get('model', '') for x in inventory if x.get('model') != model]
    output = []
    for original in documents:
        doc = copy.deepcopy(original)
        # 型号只以表格单元格的形式存在时，"这一页出现了别的型号"不能当成本页属于别的
        # 产品——多规格合册的两个规格本来就共处一页，否则它们会互相把对方的参数页整页
        # 排除。判定只看**结构化表格里有没有这个型号**：2026-10-03 批2 的水槽就是
        # ``tables[0].rows[0] = '性能参数 型号 SSW-420-2S SSW-600-2S'``，而同页的 18 个
        # region 因为按列宽切碎认不出 ``SSW-420-2S``、视觉文本又同时含两个规格，于是
        # region 分支选空、``_specification_fallback`` 再以"本页有别的型号"返回空串，
        # 整页被删、参数抽取根本没发起。
        # 这里**不再要求"文字层认不出"**：文字层（含视觉识别文本）认出型号恰恰说明这一页
        # 确实讲它；把"表格里有"作为充分条件，才切得开同页的两个规格（表格列投影负责分列）。
        structured_model_anchor = (
            bool(model)
            and _table_cells_have_model(doc, model)
        )
        pages = []
        active = False
        running_headers = {r.get('text', '').strip() for p in doc.get('pages', []) for r in p.get('regions', []) if sum(any(x.get('text', '').strip() == r.get('text', '').strip() for x in q.get('regions', [])) for q in doc.get('pages', [])) > 1 and r.get('label') in {'section_header', 'page_header'}}
        for page in doc.get('pages', []):
            original_text = str(page.get('text', ''))
            text = original_text
            regions = page.get('regions') or []
            if regions:
                selected = []
                for region in regions:
                    value = str(region.get('text', ''))
                    if value.strip() in running_headers or region.get('label') in {'page_header', 'page_footer'}:
                        continue
                    if 'table' in str(region.get('label', '')).lower() and any(contains_model(value, m) for m in [model, *other_models]):
                        value = project_markdown(value, model, other_models, region.get('cells'))
                        if not value:
                            active = False
                    own = contains_model(value, model)
                    foreign = any(contains_model(value, m) for m in other_models)
                    if own:
                        active = True
                    elif foreign or (str(region.get('label', '')) in {'heading', 'section_header', 'title'} and not re.search(r'技术参数|参数|主要特点|产品特点|technical|specifications|features', value, re.I)):
                        active = False
                    if active:
                        selected.append(value)
                text = ('\n'.join(selected)
                        or _specification_fallback(original_text, other_models, structured_model_anchor))
            elif contains_model(text, model):
                lines = text.splitlines()

                # Detect horizontal comparison tables: if multiple models appear
                # consecutively in a small region, it's likely a table header.
                # In that case, don't filter parameter rows.
                if other_models:
                    first_model_line = next((i for i, line in enumerate(lines)
                        if contains_model(line, model) or any(contains_model(line, m) for m in other_models)), None)

                    if first_model_line is not None:
                        window_end = min(first_model_line + 10, len(lines))
                        models_in_window = sum(
                            1 for line in lines[first_model_line:window_end]
                            if contains_model(line, model) or any(contains_model(line, m) for m in other_models)
                        )
                        is_horizontal_table = models_in_window >= 3
                    else:
                        is_horizontal_table = False
                else:
                    is_horizontal_table = False

                if is_horizontal_table:
                    # Horizontal comparison table: keep all content (no filtering)
                    pass
                else:
                    # Vertical layout: filter by model ownership
                    selected, active = [], False
                    for line in lines:
                        own = contains_model(line, model)
                        foreign = any(contains_model(line, m) for m in other_models)
                        if own:
                            active = True
                        elif foreign:
                            active = False
                        if active:
                            selected.append(line)
                    text = ('\n'.join(selected)
                            or _specification_fallback(original_text, other_models, structured_model_anchor))
            else:
                text = _specification_fallback(original_text, other_models, structured_model_anchor)
            if not text.strip():
                continue
            page['text'] = text
            page.pop('regions', None)
            # Broad visual fields can otherwise reintroduce another product.
            page.pop('visual_fields', None)
            pages.append(page)
        doc['pages'] = pages
        tables, previous_header, previous_page = [], None, None
        for t in doc.get('tables', []):
            raw_rows = t.get('rows', [])
            match = re.search(r'page:(\d+)', t.get('source_location') or '')
            number = int(match.group(1)) if match else None
            header = next((r for r in raw_rows if any(contains_model(' '.join(str(c or '') for c in r), m) for m in [model, *other_models])), None)
            original_page = next((p for p in original.get('pages', []) if p.get('page', p.get('number')) == number), {})
            continuation = re.search(r'续表|table\s*\(?continued\)?', str(t.get('caption', '')) + str(original_page.get('text', '')), re.I)
            if header is None and continuation and previous_header and number == (previous_page or 0) + 1 and all(len(r) == len(previous_header) for r in raw_rows):
                raw_rows = [previous_header, *expand_cells(raw_rows, t.get('cells'))]
                cells = []
            else:
                cells = t.get('cells')
            rows = project_table(raw_rows, model, other_models, cells)
            if rows:
                tables.append({**t, 'rows': rows, 'cells': []})
            previous_header, previous_page = header, number
        doc['tables'] = tables
        if pages or doc['tables']:
            output.append(doc)
    return output
