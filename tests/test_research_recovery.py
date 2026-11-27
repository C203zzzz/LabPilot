from concurrent.futures import Future

from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_research import ProcurementResearch, ResearchStopped


def seed(tmp_path):
    repo = SQLiteRepository(tmp_path / 'research.sqlite3')
    service = ProcurementResearch(repo, None, None)
    repo.put('tasks', 'task', {'id': 'task', 'baseline_version': 1, 'procurement_status': 'running'})
    run = {'id': 'run', 'task_id': 'task', 'kind': 'research', 'status': 'running',
           'baseline': {'version': 1}, 'created_at': '2026-01-01T00:00:00+00:00',
           'prices': [{'unit_price': 123}], 'usage': {'search': 4}}
    repo.put('procurement_records', 'run', run, task_id='task')
    return repo, service, run


def test_done_worker_without_lease_becomes_resumable_and_retains_results(tmp_path):
    repo, service, run = seed(tmp_path)
    future = Future(); future.set_result(None)
    service.futures['run'] = future
    recovered = service.get('task', 'run')
    assert recovered['status'] == 'interrupted'
    assert recovered['prices'] == run['prices']
    assert recovered['usage'] == run['usage']
    assert recovered['finished_at']
    assert repo.get('tasks', 'task')['procurement_status'] == 'interrupted'
    assert service.baseline('task')['run']['status'] == 'interrupted'
    service.close()


def test_cancellation_records_stop_time(tmp_path):
    repo, service, _ = seed(tmp_path)
    stopped = service.cancel('task', 'run')
    assert stopped['status'] == 'cancelled'
    assert stopped['finished_at']
    assert service.cancel('task', 'run')['finished_at'] == stopped['finished_at']
    service.close()


def test_done_old_worker_cannot_interrupt_new_live_worker(tmp_path):
    repo, service, _ = seed(tmp_path)
    future = Future(); future.set_result(None)
    service.futures['run'] = future
    lease = repo.claim_analysis('research:run')
    assert service.get('task', 'run')['status'] == 'running'
    assert repo.active_analysis('research:run') == lease
    repo.release_analysis('research:run', lease)
    service.close()


def test_pending_worker_and_user_cancellation_are_not_rewritten(tmp_path):
    repo, service, run = seed(tmp_path)
    future = Future()
    service.futures['run'] = future
    assert service.get('task', 'run')['status'] == 'running'
    future.set_result(None)
    repo.put('procurement_records', 'run', {**run, 'status': 'cancelled'}, task_id='task')
    assert service.get('task', 'run')['status'] == 'cancelled'
    service.close()


def test_lease_loss_is_saved_as_interruption_not_silent_running(tmp_path, monkeypatch):
    repo, service, run = seed(tmp_path)
    run.update(lanes={'price': 'completed', 'alternatives': 'completed'})
    repo.put('procurement_records', 'run', run, task_id='task')
    service.execution.claim = ('research:run', 'expired-owner')
    monkeypatch.setattr(service, 'persist', lambda _run: (_ for _ in ()).throw(ResearchStopped('租约已失效')))
    service._execute('task', 'run')
    assert repo.get('procurement_records', 'run')['status'] == 'interrupted'
    service.close()


def test_budget_stop_reopens_lane_marked_running(tmp_path, monkeypatch):
    repo, service, run = seed(tmp_path)
    run.update(
        status='running', active_lane='alternatives',
        baseline={'version': 1, 'model': 'Q10', 'scenario': '采购', 'parameters': []},
        baseline_id='baseline', product_source_checked=True,
        lanes={'price': 'completed', 'alternatives': 'pending'},
        alternatives=[], sources=[], gaps=[], completed_queries=[], prices=[],
        price_leads=[], limits={'search': 8, 'fetch': 8, 'model': 8}, usage={'search': 0, 'fetch': 0, 'model': 0},
    )
    repo.put('procurement_records', 'run', run, task_id='task')
    monkeypatch.setattr(service, 'alternatives', lambda _run: (_ for _ in ()).throw(ResearchStopped('额度已用完')))
    monkeypatch.setattr('backend.services.research_planning.discovery_queries', lambda _baseline: [])
    monkeypatch.setattr(service, 'recover_sources', lambda _run: None)
    monkeypatch.setattr(service, 'report', lambda *_args: None)
    service._execute('task', 'run')
    saved = repo.get('procurement_records', 'run')
    assert saved['status'] == 'partial', saved
    assert saved['lanes'] == {'price': 'completed', 'alternatives': 'pending'}
    service.close()


def test_closeout_exception_keeps_completed_lanes_as_partial_and_records_failure(tmp_path, monkeypatch):
    repo, service, run = seed(tmp_path)
    run.update(
        status='running',
        baseline={'version': 1, 'brand': '甲厂', 'model': 'Q10',
                  'category': '培养箱', 'scenario': '采购', 'parameters': []},
        product_source_checked=True,
        lanes={'price': 'completed', 'alternatives': 'completed'},
        sources=[], alternatives=[{'model': 'Q20', 'status': 'eligible'}],
        gaps=[], unresolved_source_errors=False,
        prices=[{'url': 'https://example.com/q10', 'model': 'Q10', 'unit_price': 123}],
    )
    repo.put('procurement_records', 'run', run, task_id='task')
    monkeypatch.setattr(service, 'recover_sources', lambda _run: None)
    monkeypatch.setattr(service, 'backfill_publication_attachments', lambda _run: None)
    monkeypatch.setattr(
        service, '_rescan_late_price_sources',
        lambda *_args: (_ for _ in ()).throw(
            ValueError('读取失败 https://broken.example/late?token=secret')))
    monkeypatch.setattr(service, 'report', lambda *_args: None)
    monkeypatch.setattr('backend.services.product_configuration.hydrate_run',
                        lambda _run: None)

    service._execute('task', 'run')

    saved = repo.get('procurement_records', 'run')
    assert saved['status'] == 'partial'
    assert saved['stage'] == '结果已保存'
    assert saved['lanes'] == {'price': 'completed', 'alternatives': 'completed'}
    assert saved['prices'] == run['prices']
    assert saved['failures'][-1]['stage'] == '研究收尾'
    assert saved['failures'][-1]['type'] == 'ValueError'
    assert 'broken.example/late' in saved['failures'][-1]['error']
    assert 'token=secret' not in saved['failures'][-1]['error']
    assert any('研究收尾未完全完成' in gap for gap in saved['gaps'])
    service.close()
