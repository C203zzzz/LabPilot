"""Compare local parser output to visually reviewed development annotations.

No network/model calls; do not call this a business blind test or human gold set.
Run after creating the two structured-v6 JSON development artifacts.
"""
import json
import re
from pathlib import Path
from backend.services.evidence_ownership import project_markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/reliability-development-20260919'


def main():
    annotations = json.loads((ROOT/'tests/fixtures/development_table_annotations.json').read_text(encoding='utf-8'))
    results = []
    compact = lambda s: re.sub(r'\s+', '', str(s))
    for name, doc in zip(('yiheng', 'zhichu'), annotations['documents']):
        parsed = json.loads((OUT/(name+'-structured-v6.json')).read_text(encoding='utf-8'))
        for model, values in doc['rows'].items():
            projected = '\n'.join(project_markdown(r['text'], model, [m for m in doc['rows'] if m != model], r.get('cells')) for r in parsed['regions'] if r.get('cells'))
            for field, expected, unit in zip(doc['fields'], values, doc['units']):
                row = next((line for line in projected.splitlines() if field in line), '')
                actual = row.split('|')[-1].strip() if row else None
                results.append({'document': doc['name'], 'page': doc['page'], 'region': doc['region'], 'model': model,
                                'field': field, 'unit': unit, 'expected': expected, 'actual': actual,
                                'status': 'matched' if actual and compact(expected) in compact(actual) else 'unknown' if not actual else 'mismatch'})
    summary = {s: sum(r['status'] == s for r in results) for s in ('matched', 'unknown', 'mismatch')}
    report = {'scope': annotations['scope'], 'summary': summary, 'results': results,
              'known_unreviewed': [d['unknown_or_unreviewed'] for d in annotations['documents']]}
    (OUT/'table-regression.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
