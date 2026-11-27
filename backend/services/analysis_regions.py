"""Finite splitting of model-owned evidence, preserving source anchors."""
import copy


def split_evidence(payload):
    units = []
    for doc in payload.get('documents', []):
        for kind in ('pages', 'tables'):
            for element in doc.get(kind, []):
                units.append({**doc, 'pages': [], 'tables': [], kind: [element]})
    if len(units) > 1:
        middle = len(units) // 2
        return [{**payload, 'documents': group} for group in (units[:middle], units[middle:])]
    if not units:
        return []
    doc = units[0]
    if doc.get('pages'):
        text = doc['pages'][0].get('text', '')
        if len(text) < 100:
            return []
        middle = text.rfind('\n', len(text)//3, len(text)*2//3)
        middle = middle if middle > 0 else len(text)//2
        result = []
        for text_part in (text[:middle], text[middle:]):
            part = copy.deepcopy(payload)
            part['documents'][0]['pages'][0]['text'] = text_part
            result.append(part)
        return result
    rows = doc['tables'][0].get('rows', [])
    if len(rows) < 3:
        return []
    middle = 1 + (len(rows)-1)//2
    return [{**payload, 'documents': [{**doc, 'tables': [{**doc['tables'][0], 'rows': group}]}]}
            for group in (rows[:middle], [rows[0], *rows[middle:]])]
