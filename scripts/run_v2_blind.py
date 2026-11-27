"""Phased real-provider blind workflow. No synthetic baseline or imported URLs."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/v2-blind-20260920'
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def fingerprint():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('backend', 'frontend/src', 'tests') for p in (ROOT/folder).rglob('*')
            if p.is_file() and p.suffix in ('.py', '.jsx', '.js', '.css')}


def prepare(batch):
    dest = OUT/f'batch-{batch:02d}'
    if (dest/'manifest.json').exists():
        raise RuntimeError('Batch already prepared; originals are immutable')
    inv = read(OUT/'zip-inventory.json')
    used = {}
    for p in OUT.glob('batch-*/manifest.json'):
        for item in read(p)['files']:
            used[item['member']] = item
    known = {i['sha256']: i['file'] for i in read(ROOT/'output/v1-reliability-rounds-20260919/dataset-inventory.json')['files']}
    known.update({v['sha256']: k for k, v in used.items()})
    rows, new = [], 0
    with ZipFile(ROOT/'output/实验室仪器PDF测试集v2盲测集.zip') as z:
        for item in inv['members']:
            name = item['path']
            if not name.lower().endswith('.pdf') or name in used:
                continue
            if item['uncompressed_bytes'] > inv['size_threshold_bytes']:
                continue
            parts = PurePosixPath(name)
            assert not parts.is_absolute() and '..' not in parts.parts
            raw = z.read(name)
            assert len(raw) == item['uncompressed_bytes']
            sha = hashlib.sha256(raw).hexdigest()
            index = len(rows)+1
            path = dest/'originals'/f'{index:02d}'/parts.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as f:
                f.write(raw)
            path.chmod(0o444)
            duplicate = known.get(sha)
            row = {'index': index, 'member': name, 'path': str(path), 'sha256': sha,
                   'bytes': len(raw), 'scope': 'regression_duplicate' if duplicate else 'new_blind', 'duplicate_of': duplicate}
            rows.append(row)
            known[sha] = name
            if not duplicate:
                new += 1
            if new == 5:
                break
    dump(dest/'manifest.json', {'files': rows, 'new_blind_count': new, 'prepared_at': time.time()})
    dump(dest/'first/code-fingerprint.json', fingerprint())
    print(json.dumps({'batch': batch, 'new_blind_count': new, 'files': rows}, ensure_ascii=False), flush=True)


def budget_available(slots):
    ledger = ROOT/'data/provider-usage.sqlite3'
    while True:
        with sqlite3.connect(f'file:{ledger.as_posix()}?mode=ro', uri=True) as db:
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE stamp>?', (time.time()-3600,)).fetchone()[0]
        if count <= 60-slots:
            return count
        print(json.dumps({'waiting_for_natural_hourly_budget': True, 'recent': count, 'reserved_headroom': slots}), flush=True)
        time.sleep(30)


def execute(batch, index, phase, regression):
    import fitz
    from fastapi.testclient import TestClient
    from backend.main import create_app
    folder = OUT/f'batch-{batch:02d}'
    row = next(r for r in read(folder/'manifest.json')['files'] if r['index'] == index)
    scope = regression or 'first'
    dest = folder/scope/f'file-{index:02d}'
    dest.mkdir(parents=True, exist_ok=True)
    shared = OUT/'runtime'
    dbpath = shared/'blind.sqlite3'
    statepath = dest/'state.json'
    state = read(statepath) if statepath.exists() else {'sample': row, 'steps': [], 'started_at': time.time()}
    pdfpath = Path(row['path'])
    assert hashlib.sha256(pdfpath.read_bytes()).hexdigest() == row['sha256']
    frozen = folder/scope/'code-fingerprint.json'
    if not frozen.exists():
        dump(frozen, fingerprint())
    assert read(frozen) == fingerprint(), 'Code changed within frozen phase'
    os.environ['LAB_USAGE_DATABASE'] = str(ROOT/'data/provider-usage.sqlite3')
    assert os.getenv('LAB_MODEL_CALLS_PER_HOUR') == '60'
    assert os.environ.get('DEEPSEEK_API_KEY') and os.environ.get('TAVILY_API_KEY')

    def call(client, method, path, label=None, **kwargs):
        response = getattr(client, method)(path, **kwargs)
        state['steps'].append({'method': method, 'path': path, 'http_status': response.status_code, 'at': time.time()})
        dump(statepath, state)
        if label:
            dump(dest/(label+'.json'), {'http_status': response.status_code, 'body': response.json()})
        response.raise_for_status()
        return response

    if (dest/(phase+'-finished.json')).exists():
        raise RuntimeError('Phase already frozen; use a new regression directory')
    budget_available(12 if phase == 'recognize' else 40)
    app = create_app(database_path=dbpath, storage_dir=shared/'files')
    try:
        with TestClient(app) as client:
            if phase == 'recognize':
                assert 'task_id' not in state, 'Do not repeat first recognition'
                task = call(client, 'post', '/api/v1/tasks', json={'scenario': '实际采购用途未提供；本次仅识别原始资料，勿推定用途、报价或强制要求。'}).json()['task']
                state['task_id'] = task['id']; dump(statepath, state)
                prefix = '/api/v1/tasks/'+task['id']
                doc = call(client, 'post', prefix+'/documents', label='document', files={'file': (pdfpath.name, pdfpath.read_bytes(), 'application/pdf')}, data={'category': 'product'}).json()
                with fitz.open(pdfpath) as pdf:
                    state['original_pages'] = len(pdf)
                state['parsed_pages'] = len(doc.get('pages', []))
                dump(statepath, state)
                assert state['parsed_pages'] == state['original_pages'], 'Incomplete page inventory'
                result = call(client, 'post', prefix+'/analysis/deepseek', label='analysis', json={}).json()
                state['analysis_status'] = result['status']
                records = app.state.repository.list('procurement_records', task['id'])
                dump(dest/'analysis-checkpoints.json', [r for r in records if r.get('kind', '').startswith('analysis')])
                dump(dest/'documents-after-analysis.json', app.state.repository.list('documents', task['id']))
            else:
                annotation = read(folder/'annotations'/f'file-{index:02d}.json')
                assert annotation['source_sha256'] == row['sha256'] and annotation['reviewed_against_original']
                dump(dest/'confirmed-annotation.json', annotation)
                if not annotation.get('baseline'):
                    state['blocked'] = annotation['blocked_reason']
                    dump(dest/'research-finished.json', {'blocked': state['blocked'], 'time': time.time()})
                    return
                prefix = '/api/v1/tasks/'+state['task_id']
                run = call(client, 'post', prefix+'/baseline/confirm', label='confirmation', json=annotation['baseline']).json()
                rid = run['id']; state['run_id'] = rid; dump(statepath, state)
                future = app.state.research_service.futures.get(rid)
                last = None
                while future and not future.done():
                    run = app.state.research_service.get(state['task_id'], rid)
                    status = (run['stage'], str(run['usage']))
                    if status != last:
                        print(json.dumps({'file': index, 'stage': run['stage'], 'usage': run['usage']}, ensure_ascii=False), flush=True)
                        dump(dest/'research-progress.json', run); last = status
                    time.sleep(2)
                if future:
                    future.result()
                run = call(client, 'get', prefix+'/research-runs/'+rid, label='research').json()
                content = call(client, 'post', prefix+'/research-runs/'+rid+'/report').content
                (dest/'report.zip').write_bytes(content)
                with ZipFile(io.BytesIO(content)) as archive:
                    assert archive.testzip() is None
                    state['export_members'] = archive.namelist()
                history = call(client, 'get', '/api/v1/reports', label='history').json()
                assert any(i['task_id'] == state['task_id'] for i in history['items'])
                gate = call(client, 'get', prefix+'/research-runs/'+rid+'/supplement', label='supplement-gate').json()
                if gate['allowed']:
                    call(client, 'post', prefix+'/research-runs/'+rid+'/supplement/prompt', label='supplement-prompt', json={})
                    invalid = client.post(prefix+'/research-runs/'+rid+'/supplement/import', json={'report_id': 'deliberately-wrong-version'})
                    dump(dest/'supplement-import-gate.json', {'test': 'reject wrong report version; no fabricated external leads', 'http_status': invalid.status_code, 'body': invalid.json()})
                    assert invalid.status_code == 422
                state.update(status=run['status'], usage=run['usage'], lanes=run['lanes'], prices=len(run['prices']), alternatives=len(run['alternatives']), eligible=sum(a['status']=='eligible' for a in run['alternatives']), supplement_allowed=gate['allowed'])
        if phase == 'research' and state.get('run_id'):
            reopened = create_app(database_path=dbpath, storage_dir=shared/'files')
            with TestClient(reopened) as client:
                restored = call(client, 'get', prefix+'/baseline', label='reopened').json()
                assert restored['run']['id'] == state['run_id']
                assert restored['run']['prices'] == run['prices']
            state['history_reopened'] = True
        dump(dest/(phase+'-finished.json'), {'completed_at': time.time(), 'state': state})
    except Exception as exc:
        state['error'] = {'phase': phase, 'type': type(exc).__name__, 'message': str(exc)}
        dump(dest/(phase+'-finished.json'), {'failed_at': time.time(), 'state': state})
        raise
    finally:
        dump(statepath, state)
        print(json.dumps({k: v for k, v in state.items() if k not in {'sample', 'steps', 'export_members'}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'recognize', 'research'])
    parser.add_argument('--batch', type=int, required=True)
    parser.add_argument('--file', type=int)
    parser.add_argument('--regression')
    args = parser.parse_args()
    if args.phase == 'prepare':
        prepare(args.batch)
    else:
        execute(args.batch, args.file, args.phase, args.regression)
