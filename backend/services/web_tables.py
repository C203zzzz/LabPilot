"""Preserve HTML cell geometry and emit auditable model-column projections."""
import re
import unicodedata
from html import escape
from html.parser import HTMLParser


class TableReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
        if self.depth != 1:
            return
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            attrs = dict(attrs)
            def span(name):
                try:
                    return max(1, min(100, int(attrs.get(name, 1))))
                except ValueError:
                    return 1
            self.cell = {'text': '', 'rows': span('rowspan'), 'cols': span('colspan')}
        elif tag == 'br' and self.cell is not None:
            self.cell['text'] += ' '

    def handle_endtag(self, tag):
        if self.depth == 1:
            if tag in ('td', 'th') and self.cell is not None:
                self.row.append(self.cell)
                self.cell = None
            elif tag == 'tr' and self.row is not None:
                self.rows.append(self.row)
                self.row = None
        if tag == 'table':
            self.depth -= 1

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell['text'] += data


def render_table(html):
    reader = TableReader()
    reader.feed(html)
    grid = {}
    right_edges = {}
    for r, row in enumerate(reader.rows):
        if r >= 1000:
            return html
        c = 0
        for cell in row:
            while (r, c) in grid:
                c += 1
            text = re.sub(r'\s+', ' ', cell['text']).strip()
            for y in range(r, r + cell['rows']):
                for x in range(c, c + cell['cols']):
                    if (y, x) in grid:
                        return html  # malformed overlap is not evidence of sharing
                    grid[y, x] = text
                    right_edges[y, x] = c + cell['cols']
                    if len(grid) > 10000:
                        return html
            c += cell['cols']
    if not grid or len(grid) > 10000:
        return html
    width = max(c for _, c in grid) + 1
    rows = [[grid.get((r, c), '') for c in range(width)] for r in range(max(r for r, _ in grid) + 1)]
    output = [' | '.join(row).rstrip(' |') for row in rows]
    header = None
    for r, row in enumerate(rows):
        if re.fullmatch(r'(?:产品)?型号|models?', row[0], re.I):
            labels = [re.sub(r'\s*\[https?://[^]]+\]', '', x).strip() for x in row[1:]]
            header = {c: x for c, x in enumerate(labels, 1) if x and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._/-]*', unicodedata.normalize('NFKC', x)) and re.search(r'\d', x) and re.search(r'[A-Za-z]', x)}
            continue
        if header and row[0] and not re.search(r'配件|转子|适配器|货号', row[0]):
            # A value spanning from a blank spacer into a model column is not
            # a row label. Keep its original cell boundary when projecting.
            first_model = min(header)
            descriptor = ' '.join(dict.fromkeys(cell for index, cell in enumerate(row[:first_model])
                                                if cell and right_edges.get((r, index), 0) <= first_model))
            for c, model in header.items():
                if row[c]:
                    output.append(f'[表格按列展开 行{r+1}列{c+1}] {model} {descriptor} {row[c]}')
    return '<p>' + '</p><p>'.join(escape(line) for line in output) + '</p>'


def preserve_tables(html):
    # Only innermost tables: layout tables must not project unrelated content.
    return re.sub(r'<table\b[^>]*>(?:(?!<table\b)[\s\S])*?</table\s*>', lambda m: render_table(m.group()), html, flags=re.I)
