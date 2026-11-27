"""Run all original v1 PDFs without replacing first-run evidence or using v2.

Each round re-runs current ingestion (shared structure cache is intentional),
checks source/region contracts, and exports provisional candidate/ownership
diagnostics. No model output is counted as human truth; no paid calls occur.
"""
import argparse
import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def worker(source, output, storage):
    from backend.services.document_ingestion import DocumentIngestionService
    from backend.services.candidate_service import extract_candidates, pages_from_document
    from backend.services.evidence_ownership import scoped_documents
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    start = time.monotonic()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    doc = DocumentIngestionService(storage).ingest_file(source, document_id='v1-' + digest[:20])
    leads = extract_candidates(pages_from_document(doc))
    inventory = [{'model': c.model} for c in leads]
    prompt_doc = {'document_id': doc.id, 'source_name': doc.source_name, 'pages': [{'page': p.number, 'text': p.text, 'regions': p.regions} for p in doc.pages], 'tables': [vars(t) for t in doc.tables]}
    ownership = []
    for c in leads:
        own = scoped_documents({'model': c.model}, [prompt_doc], inventory)
        ownership.append({'model': c.model, 'owned_pages': sum(len(d.get('pages', [])) for d in own), 'owned_tables': sum(len(d.get('tables', [])) for d in own), 'status': 'lead_only_not_truth'})
    issues = []
    if not doc.pages or not doc.extracted_text.strip():
        issues.append('empty_extraction')
    if any(p.document_id != doc.id for p in doc.pages):
        issues.append('page_document_mismatch')
    if [p.number for p in doc.pages] != list(range(1, len(doc.pages)+1)):
        issues.append('page_sequence_mismatch')
    unresolved = [p.number for p in doc.pages if p.route == 'vision']
    if unresolved:
        issues.append('visual_pages_unresolved')
    if not leads:
        issues.append('no_rule_candidate_leads')
    if any(w.severity == 'error' for w in doc.warnings):
        issues.append('parser_error_warning')
    if any(w.code == 'structure_needs_review' for w in doc.warnings):
        issues.append('structure_needs_review')
    payload = {'file': source.name, 'sha256': digest, 'seconds': round(time.monotonic()-start, 2),
               'scope': 'v1_current_parser_and_evidence_contracts_not_full_business', 'document': doc.to_dict(),
               'candidate_leads': ownership, 'issues': issues, 'unresolved_pages': unresolved,
               'paid_calls': 0, 'business_accuracy': 'not_measured_without_human_truth'}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', type=Path, default=ROOT/'output/v1-reliability-rounds-20260919/dataset-inventory.json')
    parser.add_argument('--round', type=int, choices=[1, 2, 3, 4])
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--storage', type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.output, args.storage)
        return
    if not args.round:
        parser.error('--round is required')
    inventory = json.loads(args.inventory.read_text(encoding='utf-8'))
    target = args.inventory.parent/f'round-{args.round:02d}'
    target.mkdir(parents=True, exist_ok=True)
    storage = args.inventory.parent/'shared-local-cache'
    results = []
    for item in inventory['files']:
        output = target/(item['sha256']+'.json')
        if not output.exists():
            with (target/(item['sha256']+'.log')).open('w', encoding='utf-8') as log:
                try:
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', item['path'], '--output', str(output.resolve()), '--storage', str(storage.resolve())], cwd=ROOT, stdout=log, stderr=log, timeout=600)
                    if completed.returncode and not output.exists():
                        output.write_text(json.dumps({'file': item['file'], 'sha256': item['sha256'], 'issues': ['parser_worker_failed'], 'exit_code': completed.returncode}), encoding='utf-8')
                except subprocess.TimeoutExpired:
                    output.write_text(json.dumps({'file': item['file'], 'sha256': item['sha256'], 'issues': ['parser_timeout_600s']}), encoding='utf-8')
        data = json.loads(output.read_text(encoding='utf-8'))
        doc = data.get('document', {})
        result = {'file': item['file'], 'sha256': item['sha256'], 'pages_expected': item['pages'], 'pages_actual': len(doc.get('pages', [])),
                  'issues': data.get('issues', []), 'leads': len(data.get('candidate_leads', [])), 'artifact': str(output)}
        if result['pages_actual'] != item['pages']:
            result['issues'].append('page_count_mismatch')
        results.append(result)
        manifest = {'round': args.round, 'files_completed': len(results), 'total_files': len(inventory['files']), 'scope': '开发回归，非完整业务验收', 'delivery_gate': 'blocked_pending_business_truth_and_known_issues', 'results': results}
        (target/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"round={args.round} {len(results)}/24 pages={result['pages_actual']}/{item['pages']} issues={len(result['issues'])} {item['file']}", flush=True)


if __name__ == '__main__':
    main()
