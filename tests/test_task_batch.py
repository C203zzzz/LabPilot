"""Batch delete / export for whole tasks in the history list.

The deletion contract is "really gone": every table that stores a row for the
task is emptied, the task disappears from ``GET /tasks``, other tasks keep all
their rows, and only files the deleted task provably owns are removed.
"""
import io
import json
from datetime import datetime, timezone
from zipfile import ZipFile

from fastapi.testclient import TestClient

from backend.domain.history import TaskVersion
from backend.main import create_app
from backend.repositories.sqlite import TASK_SCOPED_TABLES

API = '/api/v1'


def build(tmp_path):
    return create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')


def scoped_keys(repo, table, task_id):
    """Keys that identify this task's rows, including research lease aliases."""
    keys = [task_id]
    if table == 'analysis_leases':
        keys += [f"research:{row['id']}" for row in repo.db.execute(
            'SELECT id FROM procurement_records WHERE task_id = ?', (task_id,))]
    return keys


def count_rows(repo, table, keys):
    column = 'id' if table == 'tasks' else 'task_id'
    placeholders = ','.join('?' * len(keys))
    return repo.db.execute(f'SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})', keys).fetchone()[0]


def snapshot_counts(repo, task_id):
    return {table: count_rows(repo, table, scoped_keys(repo, table, task_id)) for table in TASK_SCOPED_TABLES}


def seed_task(repo, task_id, *, report_ids, document_ids):
    """Write one row into every task-scoped table, through the real API surface."""
    repo.put('tasks', task_id, {'id': task_id, 'scenario': f'采购{task_id}', 'status': 'draft'})
    repo.put('requirements', task_id, {'scenario': task_id, 'confirmed_at': '2026-09-18T00:00:00+00:00'})
    repo.put('workflow_state', task_id, {'stage': 'report'})
    for document_id in document_ids:
        repo.put('documents', document_id, {'id': document_id, 'source_name': f'{document_id}.pdf'}, task_id=task_id)
    repo.put('candidates', f'{task_id}-cand', {'id': f'{task_id}-cand', 'brand': '中科中佳', 'model': 'H1850R'}, task_id=task_id)
    for index, report_id in enumerate(report_ids, 1):
        report = {'report_id': report_id, 'sections': [{'title': '需求', 'summary': '检测吸光度', 'items': []}]}
        repo.put('reports', report_id, {
            'report': report,
            'history_version': {'version_number': index, 'created_at': f'2026-09-1{index}T16:00:00+00:00'},
        }, task_id=task_id)
    repo.put('search_runs', f'{task_id}-run', {'id': f'{task_id}-run'}, task_id=task_id)
    repo.put('source_decisions', f'{task_id}-src', {'id': f'{task_id}-src', 'url': 'https://example.com/spec'}, task_id=task_id)
    repo.put('final_decisions', f'{task_id}-final', {'decision_id': f'{task_id}-final'}, task_id=task_id)
    repo.put('supplier_inquiries', f'{task_id}-inq', {'inquiry_id': f'{task_id}-inq'}, task_id=task_id)
    repo.put('supplier_replies', f'{task_id}-rep', {'reply_id': f'{task_id}-rep'}, task_id=task_id)
    repo.put('facts', f'{task_id}-fact', {'fact_id': f'{task_id}-fact'}, task_id=task_id)
    repo.put('procurement_records', f'{task_id}-base', {'id': f'{task_id}-base', 'kind': 'baseline', 'status': 'confirmed'}, task_id=task_id)
    repo.put('province_award_cache', f'{task_id}-award', {'id': f'{task_id}-award'}, task_id=task_id)
    repo.save_archive(f'{task_id}-archive', task_id, {'task': {'id': task_id}}, '2026-09-20T00:00:00+00:00')
    repo.replace_rag_chunks(task_id, [{'chunk_id': 'chunk-1', 'text': '正文'}])
    repo.save(TaskVersion(
        task_id=task_id, version_number=1, parent_version_id=None, revision_reason='初次生成',
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc), requirement={'scenario': task_id},
        candidates=(), report_version_id=f'{task_id}-report', report_version_number=1,
        report_content_hash='c' * 64,
    ))
    repo.claim_analysis(task_id)
    repo.claim_analysis(f'research:{task_id}-base')


def test_the_registry_covers_every_table_that_stores_task_rows(tmp_path):
    app = build(tmp_path)
    with TestClient(app):
        repo = app.state.repository
        schema_tables = set(repo.task_scoped_tables())
        assert schema_tables == set(TASK_SCOPED_TABLES)
        # 18 张表里除 tasks 之外的 17 张都带 task_id 列，这里把数字固定下来，
        # 将来新增按任务存储的表就必须同时更新清单。
        assert len(schema_tables) == 18
        assert 'tasks' in schema_tables


def test_deleting_a_task_empties_every_table_and_spares_the_others(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        repo = app.state.repository
        seed_task(repo, 'task-a', report_ids=['report-a1', 'report-a2'], document_ids=['doc-a1'])
        seed_task(repo, 'task-b', report_ids=['report-b1'], document_ids=['doc-b1'])
        before_a = snapshot_counts(repo, 'task-a')
        before_b = snapshot_counts(repo, 'task-b')
        assert all(count > 0 for count in before_a.values()), before_a
        assert all(count > 0 for count in before_b.values()), before_b

        keys_a = {table: scoped_keys(repo, table, 'task-a') for table in TASK_SCOPED_TABLES}
        response = client.post(f'{API}/tasks/delete', json={'task_ids': ['task-a']})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['deleted_tasks'] == 1
        assert set(body['deleted']) == set(TASK_SCOPED_TABLES)
        assert body['deleted'] == before_a

        for table in TASK_SCOPED_TABLES:
            assert count_rows(repo, table, keys_a[table]) == 0, f'{table} still holds task-a rows'
        assert snapshot_counts(repo, 'task-b') == before_b
        remaining = [item['id'] for item in client.get(f'{API}/tasks').json()['items']]
        assert remaining == ['task-b']
        assert [item['id'] for item in client.get(f'{API}/reports').json()['items']] == ['report-b1']


def test_repeated_deletes_are_idempotent_and_stale_ids_are_reported(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        repo = app.state.repository
        seed_task(repo, 'task-a', report_ids=['report-a1'], document_ids=['doc-a1'])
        seed_task(repo, 'task-b', report_ids=['report-b1'], document_ids=['doc-b1'])

        assert client.post(f'{API}/tasks/delete', json={'task_ids': ['task-a']}).status_code == 200
        # 仓储层重复删不报错，只是再没有行可删。
        again = repo.delete_tasks(['task-a'])
        assert sum(again.values()) == 0
        assert set(again) == set(TASK_SCOPED_TABLES)

        stale = client.post(f'{API}/tasks/delete', json={'task_ids': ['task-a']})
        assert stale.status_code == 409
        assert stale.json()['detail']['code'] == 'task_selection_stale'
        # 混着不存在的 id 也要整批拒绝，不能静默忽略。
        mixed = client.post(f'{API}/tasks/delete', json={'task_ids': ['task-b', 'task-gone']})
        assert mixed.status_code == 409
        assert mixed.json()['detail']['code'] == 'task_selection_stale'
        assert [item['id'] for item in client.get(f'{API}/tasks').json()['items']] == ['task-b']


def test_task_export_zips_every_report_of_the_selected_tasks(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        repo = app.state.repository
        seed_task(repo, 'task-a', report_ids=['report-a1', 'report-a2'], document_ids=['doc-a1'])
        seed_task(repo, 'task-b', report_ids=['report-b1'], document_ids=['doc-b1'])

        exported = client.post(f'{API}/tasks/export', json={'task_ids': ['task-a']})
        assert exported.status_code == 200, exported.text
        assert exported.headers['content-type'] == 'application/zip'
        assert exported.headers['content-disposition'] == 'attachment; filename="labpilot-tasks-reports.zip"'
        with ZipFile(io.BytesIO(exported.content)) as archive:
            members = archive.namelist()
            payloads = [json.loads(archive.read(name)) for name in members if name.endswith('.json') and not name.endswith('-来源清单.json')]
        assert sorted(payload['report']['report_id'] for payload in payloads) == ['report-a1', 'report-a2']
        assert all('report-b1' not in name for name in members)

        both = client.post(f'{API}/tasks/export', json={'task_ids': ['task-a', 'task-b']})
        with ZipFile(io.BytesIO(both.content)) as archive:
            everything = sorted(name for name in archive.namelist() if name.endswith('.json') and not name.endswith('-来源清单.json'))
        assert len(everything) == 3


def test_empty_selection_and_reportless_tasks_are_reported_not_faked(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        repo = app.state.repository
        seed_task(repo, 'task-c', report_ids=[], document_ids=['doc-c1'])
        assert client.post(f'{API}/tasks/export', json={'task_ids': []}).status_code == 422
        assert client.post(f'{API}/tasks/delete', json={'task_ids': []}).status_code == 422
        empty = client.post(f'{API}/tasks/export', json={'task_ids': ['task-c']})
        assert empty.status_code == 409
        assert empty.json()['detail']['code'] == 'task_reports_empty'
        assert [item['id'] for item in client.get(f'{API}/tasks').json()['items']] == ['task-c']
        assert client.post(f'{API}/tasks/export', json={'task_ids': ['task-gone']}).status_code == 409


def test_delete_removes_only_files_the_task_owns(tmp_path):
    app = build(tmp_path)
    storage = tmp_path / 'files'
    with TestClient(app) as client:
        repo = app.state.repository
        seed_task(repo, 'task-a', report_ids=['report-a1'], document_ids=['doc-a1'])
        seed_task(repo, 'task-b', report_ids=['report-b1'], document_ids=['doc-b1'])

        # 内容寻址的两处存储：同一份字节可能同时属于别的任务，删除时必须留下。
        originals = storage / 'originals'
        originals.mkdir(parents=True, exist_ok=True)
        shared_original = originals / ('a' * 64 + '.pdf')
        shared_original.write_bytes(b'shared upload bytes')
        snapshots = tmp_path / 'research-snapshots'
        snapshots.mkdir(parents=True, exist_ok=True)
        shared_snapshot = snapshots / ('b' * 64 + '.bin')
        shared_snapshot.write_bytes(b'fetched page bytes')

        # 任务自己那份：OCR 中间产物目录 + 上传时留在 incoming 的副本。
        vision_a = storage / 'visual_inputs' / 'doc-a1'
        vision_a.mkdir(parents=True, exist_ok=True)
        (vision_a / 'page-1.png').write_bytes(b'png')
        incoming = storage / 'incoming'
        incoming.mkdir(parents=True, exist_ok=True)
        upload_a = incoming / 'upload-a1.pdf'
        upload_a.write_bytes(b'raw upload')
        repo.put('documents', 'doc-a1', {'id': 'doc-a1', 'source_name': 'a.pdf', 'source_uri': str(upload_a),
                                        'original_path': str(shared_original)}, task_id='task-a')

        vision_b = storage / 'visual_inputs' / 'doc-b1'
        vision_b.mkdir(parents=True, exist_ok=True)
        (vision_b / 'page-1.png').write_bytes(b'png')

        response = client.post(f'{API}/tasks/delete', json={'task_ids': ['task-a']})
        assert response.status_code == 200, response.text
        assert response.json()['removed_files'] == 2
        assert not vision_a.exists()
        assert not upload_a.exists()
        assert shared_original.exists()
        assert shared_snapshot.exists()
        assert vision_b.exists()
