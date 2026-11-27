"""Real API workflow regression for the recorded v1 business failure.

Uses the normal providers and the existing hourly ledger. No fake responses,
no access to v2, no mutation of the user's original task or first-run reports.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', type=int, choices=[1,2,3,4], required=True)
    parser.add_argument('--batch', default='', help='New repair batch; preserves every previous round')
    parser.add_argument('--wait-for-budget', action='store_true', help='Wait for natural hourly ledger expiry; never reset it')
    args = parser.parse_args()
    os.chdir(ROOT)
    # Always use the real ledger, irrespective of test fixtures in other processes.
    os.environ['LAB_USAGE_DATABASE'] = str(ROOT/'data/provider-usage.sqlite3')
    hourly_limit = int(os.getenv('LAB_MODEL_CALLS_PER_HOUR', '20'))
    if hourly_limit > 60:
        raise RuntimeError('超过本次授权的60次每小时验证上限')
    if args.wait_for_budget:
        while True:
            with sqlite3.connect('file:data/provider-usage.sqlite3?mode=ro', uri=True) as ledger:
                recent = ledger.execute('SELECT COUNT(*) FROM attempts WHERE provider=? AND stamp>?',
                                        (os.getenv('DEEPSEEK_ENDPOINT', 'https://api.deepseek.com/chat/completions'), time.time()-3600)).fetchone()[0]
            # Later rounds exercise the exact successful cache as well as the
            # workflow. Keep four slots for unexpected misses; the provider's
            # unchanged hard cap still guards every actual request.
            if recent <= hourly_limit - (18 if args.round == 1 else 4):
                break
            print(json.dumps({'waiting_for_natural_budget_expiry': True, 'hourly_calls': recent},ensure_ascii=False), flush=True)
            time.sleep(30)
    corpus = ROOT/'output/v1-reliability-rounds-20260919'
    if args.batch and not __import__('re').fullmatch(r'[a-zA-Z0-9_-]{1,48}', args.batch):
        raise ValueError('批次名称只能使用字母数字下划线及短横线')
    batch = corpus/args.batch if args.batch else corpus
    out = batch/f'business-round-{args.round:02d}'
    out.mkdir(parents=True, exist_ok=False)
    inv = json.loads((corpus/'dataset-inventory.json').read_text(encoding='utf-8'))
    original = sqlite3.connect('file:data/app.sqlite3?mode=ro', uri=True)
    old = json.loads(original.execute('SELECT payload FROM procurement_records WHERE id=?', ('research-18dbacc631ad4ba6ae92542369dd8e3f',)).fetchone()[0])
    docs = [json.loads(r[0]) for r in original.execute('SELECT payload FROM documents WHERE task_id=?', (old['task_id'],))]
    sample = next(item for item in inv['files'] if any(d.get('sha256')==item['sha256'] for d in docs))
    pdf = Path(sample['path'])
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == sample['sha256']
    baseline = {k:v for k,v in old['baseline'].items() if k in ('brand','model','configuration','scenario','supplier_price','quantity','preferences','currency','tax','installation','warranty','delivery','parameters','category')}
    # Reuse the prior human-confirmed requirement, not the old document citations.
    baseline['parameters'] = [{k:v for k,v in p.items() if k != 'sources'} for p in baseline['parameters']]
    from backend.main import create_app
    from fastapi.testclient import TestClient
    shared = corpus/'real-workflow'
    app = create_app(database_path=shared/'regression.sqlite3', storage_dir=shared/'files')
    repo = app.state.repository
    # Only already-existing, time-stamped caches are imported; freshness is not reset.
    for row in original.execute('SELECT id,payload FROM procurement_records'):
        record = json.loads(row[1])
        if record.get('kind') in ('search_cache','body_cache','publisher_cache') and not repo.get('procurement_records',row[0]):
            repo.put('procurement_records',row[0],record,task_id=record['task_id'])
    original.close()
    result = {'scope':'v1 recorded business failure; real providers; not blind test', 'round':args.round,
              'sample':sample, 'previous_task_id':old['task_id'], 'steps':[], 'issues':[], 'started_at':time.time()}
    fingerprint = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for folder in ('backend', 'frontend/src') for p in (ROOT/folder).rglob('*')
                   if p.is_file() and p.suffix in ('.py','.jsx','.js','.css')}
    dump(out/'code-fingerprint.json', fingerprint)
    def call(client, method, path, **kwargs):
        response = getattr(client,method)(path,**kwargs)
        result['steps'].append({'method':method,'path':path,'http_status':response.status_code})
        dump(out/'progress.json',result)
        if response.status_code >= 400:
            dump(out/'failed-response.json',response.json())
            raise RuntimeError('API failed: '+path)
        return response
    try:
        with TestClient(app) as client:
            task = call(client,'post','/api/v1/tasks',json={'scenario':baseline['scenario']}).json()['task']
            tid=task['id'];result['task_id']=tid;prefix='/api/v1/tasks/'+tid
            doc=call(client,'post',prefix+'/documents',files={'file':(pdf.name,pdf.read_bytes(),'application/pdf')},data={'category':'product'}).json()
            dump(out/'document.json',doc)
            if len(doc.get('pages', [])) != sample['pages'] or any(w.get('severity')=='error' for w in doc.get('warnings', [])):
                raise RuntimeError('上传后PDF页清单不完整或解析失败，不继续付费识别')
            analysis=call(client,'post',prefix+'/analysis/deepseek',json={}).json()
            dump(out/'analysis.json',analysis)
            from backend.services.procurement_rules import identity
            if not any(identity(c['model'])==identity(baseline['model']) for c in analysis.get('candidates',[])):
                raise RuntimeError('目标型号未正确识别，不继续确认基准')
            result['analysis_status']=analysis['status']
            nodes = repo.list('procurement_records', tid)
            result['analysis_cache_hits'] = sum(bool(n.get('scope', {}).get('cache_hit')) for n in nodes if n.get('kind')=='analysis_node')
            if analysis['status'] not in ('pending_review','completed'):
                result['issues'].append('analysis_incomplete')
            run=call(client,'post',prefix+'/baseline/confirm',json=baseline).json()
            rid=run['id'];result['run_id']=rid
            future=app.state.research_service.futures.get(rid)
            last=None
            while future and not future.done():
                current=call(client,'get',prefix+'/research-runs/'+rid).json()
                state=(current['stage'],tuple(current['usage'].values()))
                if state!=last:
                    print(json.dumps({'round':args.round,'stage':current['stage'],'usage':current['usage']},ensure_ascii=False),flush=True);last=state
                time.sleep(2)
            if future:future.result()
            run=call(client,'get',prefix+'/research-runs/'+rid).json();dump(out/'research.json',run)
            exported=call(client,'post',prefix+'/research-runs/'+rid+'/report')
            (out/'report.zip').write_bytes(exported.content)
            with ZipFile(io.BytesIO(exported.content)) as z:
                assert z.testzip() is None
                assert any(n.endswith('.md') for n in z.namelist())
                assert any(n.endswith('-来源清单.json') for n in z.namelist())
                result['export_members']=z.namelist()
            library=call(client,'get','/api/v1/reports').json()
            assert any(i['task_id']==tid for i in library['items'])
            gate=call(client,'get',prefix+'/research-runs/'+rid+'/supplement').json();dump(out/'supplement-gate.json',gate)
            if gate['allowed']:
                packet=call(client,'post',prefix+'/research-runs/'+rid+'/supplement/prompt',json={}).json()
                (out/'supplement-prompt.txt').write_text(packet['prompt'],encoding='utf-8')
            result.update(status=run['status'],usage=run['usage'],lanes=run['lanes'],prices=len(run['prices']),eligible=sum(a['status']=='eligible' for a in run['alternatives']))
            result['alternative_price_results'] = [{'brand': a.get('brand'), 'model': a['model'],
                'status': a['status'], 'prices': a.get('prices', []), 'price_search_gap': a.get('price_search_gap')}
                for a in run['alternatives']]
            result['alternative_prices'] = sum(len(a.get('prices', [])) for a in run['alternatives'])
            result['model_cache_hits'] = run.get('model_cache_hits', 0)
            result['unresolved_source_errors'] = run.get('unresolved_source_errors', False)
            result['supplement_allowed'] = gate['allowed']
            result['technical_checks_passed'] = all(v=='completed' for v in run['lanes'].values()) and not run.get('unresolved_source_errors') and not run.get('failures') and analysis['status'] in ('pending_review','completed')
            if run['status']!='completed':result['issues'].append('business_evidence_incomplete')
        # Reopen through a fresh application/repository to prove persistence.
        reopened=create_app(database_path=shared/'regression.sqlite3',storage_dir=shared/'files')
        with TestClient(reopened) as client:
            read=call(client,'get',prefix+'/baseline').json()
            assert read['run']['id']==rid
            assert read['run']['prices']==run['prices']
            again=call(client,'post',prefix+'/research-runs/'+rid+'/report')
            with ZipFile(io.BytesIO(again.content)) as z:assert z.testzip() is None
        result['workflow_completed']=True
    except Exception as exc:
        result.update(workflow_completed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        result['finished_at']=time.time()
        dump(out/'result.json',result)
        print(json.dumps({k:v for k,v in result.items() if k not in {'steps','export_members','sample'}},ensure_ascii=False),flush=True)
    if not result.get('workflow_completed') or not result.get('technical_checks_passed'):
        raise SystemExit(1)


if __name__=='__main__':main()
