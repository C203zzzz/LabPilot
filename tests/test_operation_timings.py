from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.repositories.sqlite import SQLiteRepository
from backend.services.analysis_checkpoint import AnalysisCheckpoint
from backend.services.deepseek_analysis import DeepSeekAnalysisService
from backend.services.document_ingestion import DocumentIngestionService
from backend.services.procurement_research import ProcurementResearch
from backend.services import structured_pdf
from tests.test_reliability_development import research  # noqa: F401 - shared offline fixture


@pytest.fixture
def research_service(tmp_path):
    repo = SQLiteRepository(tmp_path / 'operation-timings.db')
    repo.put('tasks', 'task-timing', {'id': 'task-timing', 'status': 'draft'})
    service = ProcurementResearch(repo, None, None)
    yield repo, service
    service.close()
    repo.close()


def _run(repo):
    run = {'id': 'research-timing', 'task_id': 'task-timing', 'kind': 'research',
           'created_at': '2026-01-01T00:00:00+00:00', 'status': 'running', 'round': 1,
           'usage': {'search': 0, 'fetch': 0, 'model': 0},
           'limits': {'search': 80, 'fetch': 160, 'model': 48},
           'baseline': {'version': 1}}
    repo.put('procurement_records', run['id'], run, task_id=run['task_id'])
    return run


def test_operation_timings_are_thread_safe_and_bounded(research_service, monkeypatch):
    repo, service = research_service
    run = _run(repo)
    monkeypatch.setattr('backend.services.procurement_research.OPERATION_TIMING_LIMIT', 400)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: service.record_operation(
            run, 'concurrent_probe', 0, __import__('time').perf_counter(), 'completed',
            sequence=index), range(400)))
    assert len(run['operation_timings']) == 400
    entries = run['operation_timings']
    assert sorted(entry['sequence'] for entry in entries) == list(range(400))
    assert all(datetime.fromisoformat(entry['started_at']).year == 1970 for entry in entries)

    service.record_operation(run, 'last', 0, __import__('time').perf_counter(), 'completed')
    entries = run['operation_timings']
    assert len(entries) == 400
    assert entries[-1]['operation'] == 'last'
    assert entries[0]['sequence'] == 1


def test_imported_pdf_timing_keeps_its_measured_duration(research_service):
    repo, service = research_service
    run = _run(repo)
    run['round'] = 3
    entry = {'operation': 'docling', 'status': 'failed',
             'started_at': '2026-01-01T00:00:00+00:00',
             'finished_at': '2026-01-01T00:01:40+00:00', 'duration_ms': 100000,
             'requested_pages': 2}
    service.import_operation(run, entry, cache='miss')
    assert run['operation_timings'] == [{**entry, 'cache': 'miss', 'round': 3}]


def test_cache_writes_and_persistence_stay_on_the_research_run(research_service):
    repo, service = research_service
    run = _run(repo)
    cache = {'id': 'body-cache-test', 'task_id': run['task_id'],
             'kind': 'body_cache', 'created_at': '2026-01-01T00:00:00+00:00'}
    service.save_cache(run, cache)
    assert 'operation_timings' not in repo.get('procurement_records', cache['id'])
    assert [(entry['operation'], entry['cache']) for entry in run['operation_timings']] == [
        ('cache_save', 'body_cache')]

    service.persist(run)
    stored = repo.get('procurement_records', run['id'])
    assert [entry['operation'] for entry in stored['operation_timings']] == [
        'cache_save', 'persist']
    assert all(entry['duration_ms'] >= 0 for entry in stored['operation_timings'])


def test_a_real_offline_round_exposes_the_operation_ledger(research):
    service, run, _, client = research
    entries = run['operation_timings']
    names = {entry['operation'] for entry in entries}
    assert {'search_cache_lookup', 'search_provider', 'model_cache_lookup',
            'model', 'cache_save', 'persist'} <= names
    assert all(entry['duration_ms'] >= 0 and entry['started_at'] <= entry['finished_at']
               for entry in entries)
    assert all(entry['round'] == run['round'] for entry in entries)
    assert any(entry['operation'] == 'model' and entry['attempts'] >= 1
               for entry in entries)
    forbidden = {'prompt', 'system_prompt', 'user_prompt', 'body_text', 'payload', 'text'}
    assert all(not (set(entry) & forbidden) for entry in entries)
    assert service.repo.get('procurement_records', run['id'])['operation_timings'] == entries
    fetched = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()
    assert fetched['operation_timings'] == entries
    task = client.get(f"/api/v1/tasks/{run['task_id']}").json()['task']
    assert task['operation_timings'] == entries


def test_docling_failure_is_timed_without_starting_a_real_worker(tmp_path, monkeypatch):
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'offline fixture, not a real PDF')
    monkeypatch.setattr(structured_pdf.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match='子进程失败'):
        structured_pdf.parse_document(source, tmp_path / 'cache', [2])
    entry = structured_pdf._OPERATION_STATE.entry
    assert entry['operation'] == 'docling'
    assert entry['status'] == 'failed'
    assert entry['cache'] == 'miss'
    assert entry['requested_pages'] == 1
    assert entry['all_pages'] is False
    assert entry['duration_ms'] >= 0


def test_document_ledger_keeps_successful_docling_and_failed_lightweight_passes(tmp_path):
    source = tmp_path / 'fixture.txt'
    source.write_text('offline fixture', encoding='utf-8')
    document = DocumentIngestionService(tmp_path).ingest_file(source)
    structured_pdf._OPERATION_STATE.entry = {
        'operation': 'docling', 'status': 'completed', 'duration_ms': 321,
        'cache': 'hit', 'started_at': '2026-01-01T00:00:00+00:00',
        'finished_at': '2026-01-01T00:00:01+00:00'}
    DocumentIngestionService._take_structured_timing(document, structured_pdf)
    assert structured_pdf._OPERATION_STATE.entry is None
    assert document.operation_timings[0]['duration_ms'] == 321
    assert document.to_dict()['operation_timings'] == document.operation_timings

    structured_pdf._OPERATION_STATE.entry = None
    with pytest.raises(Exception, match='no such file'):
        structured_pdf.native_text_regions(tmp_path / 'missing.pdf')
    entry = structured_pdf._OPERATION_STATE.entry
    assert entry['operation'] == 'native_light'
    assert entry['status'] == 'failed'
    assert entry['pages'] == 0
    assert entry['duration_ms'] >= 0


def test_document_model_cache_hit_is_not_counted_as_a_new_call(tmp_path):
    repo = SQLiteRepository(tmp_path / 'document-model.db')

    class Model:
        calls = 0

        def analyze(self, **kwargs):
            self.calls += 1
            return {'candidates': []}

    model = Model()
    checkpoint = AnalysisCheckpoint(repo, 'task-model')
    documents = [{'id': 'doc-1', 'source_name': 'fixture.txt', 'tables': [],
                  'pages': [{'number': 1, 'source_location': 'page:1', 'text': 'offline'}]}]
    first = DeepSeekAnalysisService(model, checkpoint=checkpoint).analyze(
        scenario='采购', documents=documents)
    second = DeepSeekAnalysisService(model, checkpoint=checkpoint).analyze(
        scenario='采购', documents=documents)
    assert model.calls == 1
    assert [(entry['purpose'], entry['cache'], entry['attempts'])
            for entry in first['operation_timings']] == [('inventory', 'miss', 1)]
    assert [(entry['purpose'], entry['cache'], entry['attempts'])
            for entry in second['operation_timings']] == [('inventory', 'checkpoint_hit', 0)]
    assert all('user_prompt' not in entry and 'system_prompt' not in entry
               for entry in first['operation_timings'] + second['operation_timings'])
    assert repo.get('procurement_records', first['analysis_id']) is None
    repo.close()
