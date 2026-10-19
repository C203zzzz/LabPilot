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


def scoped_documents(candidate, documents, inventory):
    """Bound detail retrieval to owned text, including multi-model table headers.

    A continuation without a model anchor remains unknown rather than being
    borrowed from an unrelated instrument. Structured regions can retain a
    heading and its following blocks without including adjacent products.
    """
    model = candidate.get('model', '')
    other_models = [x.get('model', '') for x in inventory if x.get('model') != model]
    output = []
    for original in documents:
        doc = copy.deepcopy(original)
        pages = []
        active = False
        running_headers = {r.get('text', '').strip() for p in doc.get('pages', []) for r in p.get('regions', []) if sum(any(x.get('text', '').strip() == r.get('text', '').strip() for x in q.get('regions', [])) for q in doc.get('pages', [])) > 1 and r.get('label') in {'section_header', 'page_header'}}
        for page in doc.get('pages', []):
            text = str(page.get('text', ''))
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
                text = '\n'.join(selected)
            elif contains_model(text, model):
                lines = text.splitlines()
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
                text = '\n'.join(selected)
            else:
                text = ''
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
