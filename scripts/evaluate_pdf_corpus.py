"""Reproducible parser evaluation. No network model calls; preserves first outputs."""
import argparse, hashlib, json, time
from pathlib import Path
from backend.services.document_ingestion import DocumentIngestionService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', choices=['开发集', '盲测集'], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    root = Path('output/pdf-test-corpus/实验室仪器PDF测试集') / args.split
    service = DocumentIngestionService(target / 'storage')
    manifest = []
    for source in sorted(root.glob('*.pdf')):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result_file = target / (digest + '.json')
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding='utf-8'))
        else:
            start = time.monotonic()
            try:
                doc = service.ingest_file(source)
                result = {'file': source.name, 'sha256': digest, 'seconds': round(time.monotonic()-start, 2), 'document': doc.to_dict(), 'scope': 'parser_only', 'accuracy': 'not_measured'}
            except Exception as exc:
                result = {'file': source.name, 'sha256': digest, 'error_type': type(exc).__name__, 'scope': 'parser_only', 'accuracy': 'not_measured'}
            result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        doc = result.get('document', {})
        manifest.append({'file': source.name, 'artifact': str(result_file), 'pages': len(doc.get('pages', [])), 'characters': len(doc.get('extracted_text', '')), 'warnings': doc.get('warnings', []), 'accuracy': 'not_measured'})
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(source.name, len(doc.get('pages', [])), flush=True)

if __name__ == '__main__':
    main()
