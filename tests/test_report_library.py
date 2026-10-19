import io
import json
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.services.task_title import task_title


def test_titles_identify_subject_without_using_instructions():
    assert task_title({'scenario': '采购酶标仪，预算五万元，每天检测100份样品'}) == '酶标仪选型'
    assert task_title({'scenario': '从上传资料中识别可独立采购的实验室仪器完整型号'}) == '实验室仪器选型'
    assert task_title({'scenario': '从资料识别仪器'}, [{'model': 'X100', 'sources': [{'excerpt': '高速冷冻离心机 X100'}]}]) == '高速冷冻离心机选型'
    assert task_title({'scenario': '选型'}, [{'model': 'X100', 'status': 'excluded'}]) == '实验室仪器选型'
    assert task_title({'scenario': '选型'}, documents=[{'source_name': '多功能酶标仪说明书.pdf'}]) == '多功能酶标仪选型'


def seed(app):
    repo = app.state.repository
    repo.put('tasks', 'task-a', {'id': 'task-a', 'scenario': '采购酶标仪，预算五万元', 'status': 'draft'})
    repo.put('documents', 'doc-a', {'id': 'doc-a', 'source_name': '原始资料.pdf'}, task_id='task-a')
    for index, stamp in [(1, '2026-09-18T16:00:00+00:00'), (2, '2026-09-17T10:00:00+00:00')]:
        report = {'report_id': f'report-{index}', 'sections': [{'title': '需求', 'summary': '检测吸光度', 'items': [{'label': '用途', 'text': '微生物检测', 'evidence': [{'url': 'https://example.com/spec', 'location': '正文'}]}]}]}
        repo.put('reports', f'report-{index}', {'report': report, 'history_version': {'version_number': index, 'created_at': stamp}}, task_id='task-a')
    repo.put('reports', 'plan-a', {'kind': 'search_plan'}, task_id='task-a')


def test_generic_tasks_use_document_headings_not_incidental_instruments():
    task = {'scenario': '从上传资料中识别可采购的实验室仪器完整型号与配置'}
    for text, expected in [
        ('Spark\n®\n 多功能酶标仪\n配备类似培养箱的环境控制，自动显微镜', '多功能酶标仪选型'),
        ('生化培养箱/冷藏培养箱/BOD 培养箱\n产品特点', '生化培养箱选型'),
        ('多功能微孔板检测仪（酶标仪）\nIn/f_inite', '多功能微孔板检测仪选型'),
        ('Thermo Scientific KingFisher Flex\n高通量全自动核酸提取仪', '核酸提取仪选型'),
        ('5420 小型台式高速离心机-满足常规离心应用', '高速离心机选型'),
    ]:
        assert task_title(task, documents=[{'source_name': 'a84fa0.pdf', 'extracted_text': text}]) == expected


def test_document_title_consistent_across_task_and_report_endpoints(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')
    with TestClient(app) as client:
        seed(app)
        repo = app.state.repository
        repo.put('tasks', 'task-a', {'id': 'task-a', 'scenario': '从资料识别仪器', 'status': 'draft'})
        repo.put('documents', 'doc-a', {'id': 'doc-a', 'source_name': 'random.pdf', 'extracted_text': 'Spark\n多功能酶标仪'}, task_id='task-a')
        assert client.get('/api/v1/tasks').json()['items'][0]['title'] == '多功能酶标仪选型'
        assert client.get('/api/v1/tasks/task-a').json()['task']['title'] == '多功能酶标仪选型'
        assert {r['title'] for r in client.get('/api/v1/reports').json()['items']} == {'多功能酶标仪选型'}


def test_concurrent_history_reads_and_writes(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')
    with TestClient(app) as client:
        seed(app)
        def request(index):
            app.state.repository.put('tasks', f'new-{index}', {'id': f'new-{index}', 'scenario': '采购离心机'})
            assert client.get('/api/v1/reports').status_code == 200
            assert client.get('/api/v1/tasks').status_code == 200
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(request, range(32)))
        assert len(app.state.repository.list_tasks()) == 33


def test_report_library_export_and_atomic_deletion(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')
    with TestClient(app) as client:
        seed(app)
        entries = client.get('/api/v1/reports').json()['items']
        assert len(entries) == 2
        assert entries[0]['date'] == '2026-09-19'
        assert entries[0]['categories'] == ['酶标仪']
        assert entries[0]['title'] == '酶标仪选型'
        exported = client.post('/api/v1/reports/export', json={'report_ids': ['report-1', 'report-2']})
        assert exported.status_code == 200 and exported.headers['content-type'] == 'application/zip'
        with ZipFile(io.BytesIO(exported.content)) as archive:
            assert len(archive.namelist()) == 7
            markdown = next(name for name in archive.namelist() if name.endswith('.md'))
            assert 'https://example.com/spec' in archive.read(markdown).decode()
            data = next(name for name in archive.namelist() if name.endswith('.json'))
            assert json.loads(archive.read(data))['report']['report_id'] in {'report-1', 'report-2'}
        assert client.post('/api/v1/reports/delete', json={'report_ids': ['report-1', 'missing']}).status_code == 409
        assert len(client.get('/api/v1/reports').json()['items']) == 2
        assert client.post('/api/v1/reports/delete', json={'report_ids': ['plan-a']}).status_code == 409
        assert client.post('/api/v1/reports/delete', json={'report_ids': []}).status_code == 422
        result = client.post('/api/v1/reports/delete', json={'report_ids': ['report-1', 'report-1', 'report-2']})
        assert result.json()['deleted_count'] == 2
        assert client.get('/api/v1/reports').json()['items'] == []
        assert app.state.repository.get('tasks', 'task-a')
        assert app.state.repository.get('documents', 'doc-a')
        assert app.state.repository.get('reports', 'plan-a')


def test_existing_task_titles_are_derived_without_rewriting_scenario(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')
    with TestClient(app) as client:
        seed(app)
        task = client.get('/api/v1/tasks/task-a').json()['task']
        assert task['title'] == '酶标仪选型'
        assert task['scenario'] == '采购酶标仪，预算五万元'
        assert client.get('/api/v1/tasks').json()['items'][0]['title'] == task['title']
