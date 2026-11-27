"""Real API acceptance, with immutable attempts and explicit development/blind gates.

The caller supplies one reviewed manifest and one current batch only. This tool
never discovers or opens future blind packages, invents baselines, or injects URLs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
# 验收根目录可通过环境变量指向独立语料，默认保持原有生物仪器验收目录不变。
OUTPUT = Path(os.environ.get('LAB_ACCEPTANCE_OUTPUT')
              or ROOT / 'output/bio-instrument-acceptance-20260920').resolve()
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def code_fingerprint():
    return {p.relative_to(ROOT).as_posix(): sha(p)
            for base in ('backend', 'frontend/src', 'tests', 'scripts')
            for p in sorted((ROOT/base).rglob('*'))
            if p.is_file() and p.suffix in {'.py', '.jsx', '.js', '.css'}}


def verify_gate(gate, expected_stage, fingerprint):
    if gate.get('stage') != expected_stage or gate.get('status') != 'passed':
        raise ValueError('前一阶段未通过，禁止读取后续盲测资料')
    if gate.get('code_fingerprint') != fingerprint:
        raise ValueError('验收版本已变化，先更新相关回归证据')
    if gate.get('unresolved_fixable_defects') != []:
        raise ValueError('仍有未修复缺陷')
    for key in ('original_review_completed', 'workflow_verified', 'cross_template_regression_verified', 'frontend_e2e_verified'):
        if gate.get(key) is not True:
            raise ValueError('缺少验收结论：'+key)
    evidence = gate.get('evidence', [])
    if not evidence:
        raise ValueError('验收不能只有声明，必须提供实际证据文件')
    for item in evidence:
        path = Path(item['path'])
        if not path.is_file() or sha(path) != item.get('sha256'):
            raise ValueError('验收证据丢失或被修改')


def validate_manifest(manifest, blind=False):
    files = manifest.get('files', [])
    if not files or (blind and len(files) > 5):
        raise ValueError('每个盲测批次须包含1至5份资料')
    identifiers, hashes, categories = set(), set(), set()
    for item in files:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item.get('id', '')):
            raise ValueError('文件ID必须为安全标识')
        if item['id'] in identifiers or item.get('sha256') in hashes:
            raise ValueError('批次内存在重复文件或ID')
        if not item.get('category') or (blind and item['category'] in categories):
            raise ValueError('盲测批次每份必须属于不同品类')
        if item.get('single_model_verified') is not True:
            raise ValueError('资料尚未完成单主机型号核对')
        if not isinstance(item.get('pages'), int) or item['pages'] < 1:
            raise ValueError('缺少有效原始页数')
        identifiers.add(item['id']); hashes.add(item['sha256']); categories.add(item['category'])
    return files


def stage_path(stage):
    if not re.fullmatch(r'(development|blind/batch-\d{2})/(first|regression-\d{2})', stage):
        raise ValueError('阶段路径无效')
    return OUTPUT/stage


def register(stage, manifest_path):
    target = stage_path(stage)
    if (target/'manifest.json').exists():
        raise ValueError('阶段清单已冻结，不能覆盖')
    if not stage.endswith('/first'):
        first_root = target.parent/'first'
        frozen = read(first_root/'frozen-results.json')
        for name, digest in frozen['files'].items():
            if sha(first_root/name) != digest:
                raise ValueError('首次输出已被修改，停止回归')
    fingerprint = code_fingerprint()
    blind = stage.startswith('blind/')
    if blind and stage.endswith('/first'):
        batch = int(stage.split('/')[1].split('-')[1])
        previous = 'development' if batch == 1 else f'blind/batch-{batch-1:02d}'
        verify_gate(read(OUTPUT/previous/'acceptance-gate.json'), previous, fingerprint)
    manifest = read(manifest_path)
    files = validate_manifest(manifest, blind)
    if blind and not stage.endswith('/first'):
        first = read(target.parent/'first/manifest.json')
        original_samples = {(x['id'], x['sha256'], x['category']) for x in first['files']}
        if {(x['id'], x['sha256'], x['category']) for x in files} != original_samples:
            raise ValueError('回归只能使用本批已测资料，不能加入未测盲测')
    originals = target.parent/'originals'
    known_hashes = {}
    for p in OUTPUT.glob('**/manifest.json'):
        # Only already-registered metadata, never future package contents.
        for item in read(p).get('files', []):
            known_hashes[item['sha256']] = str(p)
    v1_inventory = ROOT/'output/v1-reliability-rounds-20260919/dataset-inventory.json'
    if v1_inventory.exists():
        known_hashes.update({item['sha256']: str(v1_inventory) for item in read(v1_inventory)['files']})
    normalized = []
    for item in files:
        path = Path(item['path']).resolve()
        if path.suffix.lower() != '.pdf' or sha(path) != item['sha256']:
            raise ValueError('原始PDF路径或哈希不符')
        if path.stat().st_size > 52428800:
            raise ValueError('资料超过预先固定50MiB阈值，需另列跳过项')
        if blind and stage.endswith('/first') and item['sha256'] in known_hashes:
            raise ValueError('已接触资料不能计入新盲测')
        copy = originals/(item['id']+'.pdf')
        copy.parent.mkdir(parents=True, exist_ok=True)
        if copy.exists():
            if sha(copy) != item['sha256']:
                raise ValueError('原件ID冲突')
        else:
            with copy.open('xb') as stream:
                stream.write(path.read_bytes())
        normalized.append({**item, 'original_source_path': str(path), 'path': str(copy)})
    write(target/'manifest.json', {**manifest, 'files': normalized, 'registered_at': time.time()})
    write(target/'code-fingerprint.json', fingerprint)


def budget_snapshot():
    ledger = ROOT/'data/provider-usage.sqlite3'
    with sqlite3.connect(f'file:{ledger.as_posix()}?mode=ro', uri=True) as db:
        rows = db.execute('SELECT provider,stamp FROM attempts WHERE stamp>? ORDER BY stamp', (time.time()-3600,)).fetchall()
    return {'recorded_at': time.time(), 'hourly_limit': 60, 'recent_attempts': len(rows),
            'first_expiry_at': rows[0][1]+3600 if rows else None}


def verify_export(content):
    checked = []
    with ZipFile(io.BytesIO(content)) as archive:
        if archive.testzip() is not None:
            raise ValueError('报告ZIP损坏')
        manifests = [n for n in archive.namelist() if n.endswith('-来源清单.json')]
        if not manifests or not any(n.endswith('.md') for n in archive.namelist()):
            raise ValueError('报告缺Markdown或来源清单')
        for member in manifests:
            for entry in json.loads(archive.read(member)):
                if entry.get('original_status') == 'missing_or_invalid':
                    raise ValueError('已抓原件未完整进入导出')
                if entry.get('original_status') == 'included':
                    raw = archive.read(entry['original_file'])
                    if hashlib.sha256(raw).hexdigest() != entry['sha256'] or len(raw) != entry['size']:
                        raise ValueError('导出原件校验失败')
                    checked.append(entry['sha256'])
    return {'crc_ok': True, 'original_snapshots_verified': len(checked)}


def run(stage, identifier, phase, annotation_path=None):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    target = stage_path(stage)
    if read(target/'code-fingerprint.json') != code_fingerprint():
        raise ValueError('阶段源码已变化；修复后请注册独立回归阶段')
    if (target/'frozen-results.json').exists():
        raise ValueError('首次结果已封存，禁止修改')
    item = next(v for v in read(target/'manifest.json')['files'] if v['id'] == identifier)
    dest = target/identifier
    state = read(dest/'state.json') if (dest/'state.json').exists() else {'sample': item, 'steps': []}
    if (dest/(phase+'-result.json')).exists():
        raise ValueError('本阶段已执行；不得覆盖首次结果或失败记录')
    if sha(item['path']) != item['sha256']:
        raise ValueError('原件被修改')
    if phase in {'analyze', 'research'}:
        uploaded = read(dest/'upload-result.json')
        if uploaded.get('status') != 'phase_completed' or not state.get('task_id'):
            raise ValueError('必须先完成本阶段上传')
    if phase == 'research':
        analyzed = read(dest/'analyze-result.json')
        if analyzed.get('status') != 'phase_completed':
            raise ValueError('识别失败不能绕过进入研究')
    os.environ['LAB_USAGE_DATABASE'] = str(ROOT/'data/provider-usage.sqlite3')
    if os.environ.get('LAB_MODEL_CALLS_PER_HOUR') != '60':
        raise ValueError('验证进程小时限额必须为60')
    if not all(os.getenv(key) for key in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY')):
        raise ValueError('必须从用户环境继承真实提供商配置')
    budget = budget_snapshot()
    write(dest/(phase+'-budget-before.json'), budget)
    required_headroom = {'upload': 0, 'analyze': 12, 'research': 40}[phase]
    if budget['recent_attempts']+required_headroom > 60:
        print(json.dumps({'status': 'wait_for_natural_budget', **budget}), flush=True)
        return 75
    runtime = OUTPUT/'runtime'
    app = create_app(database_path=runtime/'acceptance.sqlite3', storage_dir=runtime/'files')
    def call(client, method, url, label, **kwargs):
        response = getattr(client, method)(url, **kwargs)
        state['steps'].append({'phase': phase, 'method': method, 'url': url, 'http_status': response.status_code, 'at': time.time()})
        if 'json' in response.headers.get('content-type', ''):
            write(dest/(label+'.json'), {'http_status': response.status_code, 'body': response.json()})
        write(dest/'state.json', state)
        response.raise_for_status()
        return response
    outcome = {'phase': phase, 'started_at': time.time(), 'status': 'running'}
    try:
        with TestClient(app) as client:
            if phase == 'upload':
                task = call(client, 'post', '/api/v1/tasks', 'task', json={'scenario': '实际采购用途未提供。本任务识别原始产品资料，不推定用途、报价或需求。'}).json()['task']
                state['task_id'] = task['id']; write(dest/'state.json', state)
                prefix = '/api/v1/tasks/'+task['id']
                doc = call(client, 'post', prefix+'/documents', 'document', files={'file': (Path(item['path']).name, Path(item['path']).read_bytes(), 'application/pdf')}, data={'category': 'product'}).json()
                if len(doc.get('pages', [])) != item['pages'] or any(w.get('severity') == 'error' for w in doc.get('warnings', [])):
                    raise ValueError('PDF页清单不完整或存在解析错误')
                outcome['pages_verified'] = item['pages']
            elif phase == 'analyze':
                prefix = '/api/v1/tasks/'+state['task_id']
                analysis = call(client, 'post', prefix+'/analysis/deepseek', 'analysis', json={}).json()
                outcome.update(analysis_status=analysis['status'], candidates=len(analysis.get('candidates', [])), gaps=analysis.get('gaps', []))
            else:
                annotation = read(annotation_path)
                if annotation.get('source_sha256') != item['sha256'] or annotation.get('reviewed_against_original') is not True:
                    raise ValueError('基准尚未独立对照原PDF核实')
                write(dest/'annotation.json', annotation)
                if not annotation.get('baseline'):
                    outcome.update(status='blocked_missing_baseline', reason=annotation.get('blocked_reason'))
                    return 2
                prefix = '/api/v1/tasks/'+state['task_id']
                research = call(client, 'post', prefix+'/baseline/confirm', 'confirmation', json=annotation['baseline']).json()
                rid = research['id']; state['run_id'] = rid; write(dest/'state.json', state)
                future = app.state.research_service.futures.get(rid)
                last = None
                while future and not future.done():
                    progress = app.state.research_service.get(state['task_id'], rid)
                    stamp = (progress['stage'], tuple(progress['usage'].values()))
                    if stamp != last:
                        print(json.dumps({'file': identifier, 'stage': progress['stage'], 'usage': progress['usage']}, ensure_ascii=False), flush=True)
                        write(dest/'research-progress.json', progress); last = stamp
                    time.sleep(2)
                if future:
                    future.result()
                research = call(client, 'get', prefix+'/research-runs/'+rid, 'research').json()
                exported = call(client, 'post', prefix+'/research-runs/'+rid+'/report', 'export').content
                (dest/'report.zip').write_bytes(exported)
                outcome['export'] = verify_export(exported)
                history = call(client, 'get', '/api/v1/reports', 'history').json()
                if not any(v['task_id'] == state['task_id'] for v in history['items']):
                    raise ValueError('报告未进入历史列表')
                gate = call(client, 'get', prefix+'/research-runs/'+rid+'/supplement', 'supplement-gate').json()
                if gate['allowed']:
                    call(client, 'post', prefix+'/research-runs/'+rid+'/supplement/prompt', 'supplement-prompt', json={})
                    response = client.post(prefix+'/research-runs/'+rid+'/supplement/import', json={'report_id': 'wrong-version'})
                    write(dest/'supplement-import-gate.json', {'status': response.status_code, 'body': response.json(), 'scope': 'version rejection only; not external-agent research'})
                    if response.status_code != 422:
                        raise ValueError('补证错误版本未被拒绝')
                outcome.update(business_status=research['status'], usage=research['usage'], lanes=research['lanes'], prices=len(research['prices']), alternatives=len(research['alternatives']), eligible=sum(v['status'] == 'eligible' for v in research['alternatives']), supplement_allowed=gate['allowed'])
            if state.get('task_id'):
                repo = app.state.repository
                write(dest/(phase+'-documents.json'), repo.list('documents', state['task_id']))
                write(dest/(phase+'-analysis-nodes.json'), [r for r in repo.list('procurement_records', state['task_id']) if r.get('kind') in {'analysis_node', 'analysis_control'}])
        if phase == 'research' and state.get('run_id'):
            reopened = create_app(database_path=runtime/'acceptance.sqlite3', storage_dir=runtime/'files')
            with TestClient(reopened) as client:
                restored = call(client, 'get', prefix+'/baseline', 'reopened').json()['run']
                if restored['id'] != state['run_id'] or restored['prices'] != research['prices']:
                    raise ValueError('历史重开结果不一致')
            outcome['reopened_verified'] = True
        outcome['status'] = 'phase_completed'
        return 0
    except Exception as exc:
        outcome.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        return 1
    finally:
        outcome['finished_at'] = time.time()
        write(dest/(phase+'-result.json'), outcome)
        write(dest/'state.json', state)
        write(dest/(phase+'-budget-after.json'), budget_snapshot())
        print(json.dumps(outcome, ensure_ascii=False), flush=True)


def freeze(stage):
    target = stage_path(stage)
    if (target/'frozen-results.json').exists():
        raise ValueError('阶段已封存')
    for item in read(target/'manifest.json')['files']:
        dest = target/item['id']
        upload = read(dest/'upload-result.json')
        if upload['status'] == 'phase_completed':
            analyze = read(dest/'analyze-result.json')
            if analyze['status'] == 'phase_completed':
                read(dest/'research-result.json')
    if read(target/'code-fingerprint.json') != code_fingerprint():
        raise ValueError('首次输出完成前代码已漂移')
    hashes = {p.relative_to(target).as_posix(): sha(p) for p in target.rglob('*') if p.is_file()}
    write(target/'frozen-results.json', {'stage': stage, 'files': hashes, 'frozen_at': time.time(), 'not_an_acceptance_gate': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['register', 'upload', 'analyze', 'research', 'freeze'])
    parser.add_argument('--stage', required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--file')
    parser.add_argument('--annotation', type=Path)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == 'register':
        register(args.stage, args.manifest)
    elif args.command == 'freeze':
        freeze(args.stage)
    else:
        raise SystemExit(run(args.stage, args.file, args.command, args.annotation))
