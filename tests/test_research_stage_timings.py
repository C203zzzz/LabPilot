"""Every research stage must leave a wall-clock record behind.

The chain that produced the acceptance timings ran for ~20 minutes with 20
model calls, and nothing in the run said which step took the time. These tests
pin the record: start, end, duration and the paid/search calls per stage, kept
on the run so the existing GET endpoints can read it back.
"""
from __future__ import annotations

import time

import pytest

from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_research import ProcurementResearch, ResearchStopped
from tests.test_reliability_development import research  # noqa: F401 - shared fixture


@pytest.fixture
def research_service(tmp_path):
    repo = SQLiteRepository(tmp_path / 'stage-timings.db')
    repo.put('tasks', 'task-timing', {'id': 'task-timing', 'status': 'draft'})
    service = ProcurementResearch(repo, None, None)
    yield repo, service
    service.close()
    repo.close()


def _run(repo, usage=None):
    run = {'id': 'research-timing', 'task_id': 'task-timing', 'kind': 'research',
           'created_at': '2026-01-01T00:00:00+00:00', 'status': 'running', 'stage': '核查同款价格',
           'round': 1, 'lanes': {'price': 'running', 'alternatives': 'pending'},
           'usage': dict(usage or {'search': 0, 'fetch': 0, 'model': 0}),
           'limits': {'search': 80, 'fetch': 160, 'model': 48}, 'baseline': {'version': 1}}
    repo.put('procurement_records', run['id'], run, task_id=run['task_id'])
    return run


def test_duration_matches_an_externally_measured_wall_clock(research_service):
    repo, service = research_service
    run = _run(repo)
    with service.stage(run, '核查同款价格'):
        time.sleep(0.25)
    entry = run['stage_timings'][0]
    # 阶段名与 run.stage 同词序：进度面板把 run.stage 印在状态行、把 name 印在已完成
    # 阶段清单里，两个词序不同就是同一步在同一屏上说两遍（``procurement_research``
    # 价格通道那段有详细说明）。
    assert entry['name'] == '核查同款价格'
    assert entry['stage'] == '核查同款价格'
    assert entry['status'] == 'completed'
    assert 200 <= entry['duration_ms'] < 2000
    assert entry['started_at'] <= entry['finished_at']


def test_calls_are_counted_from_usage_not_guessed(research_service):
    repo, service = research_service
    run = _run(repo)
    with service.stage(run, '核查其他品牌'):
        run['usage']['model'] += 2
        run['usage']['fetch'] += 1
    entry = run['stage_timings'][0]
    # Counted from usage, not guessed from the stage name. ``education_platform`` is
    # the fourth counter and ``province_award`` the fifth; both stay 0 here because
    # this stage never spends them.
    assert entry['calls'] == {'search': 0, 'fetch': 1, 'model': 2,
                              'education_platform': 0, 'province_award': 0}
    # A later stage starts counting from the round's totals, not from zero.
    with service.stage(run, '来源复核与结果汇总'):
        run['usage']['model'] += 1
    assert run['stage_timings'][1]['calls']['model'] == 1


def test_a_stopped_stage_is_still_recorded(research_service):
    repo, service = research_service
    run = _run(repo)
    with pytest.raises(ResearchStopped):
        with service.stage(run, '核查其他品牌'):
            raise ResearchStopped('本轮调用额度已用完')
    entry = run['stage_timings'][0]
    assert entry['status'] == 'stopped'
    assert entry['name'] == '核查其他品牌'


def test_the_timing_log_is_bounded_and_persisted(research_service):
    repo, service = research_service
    run = _run(repo)
    for index in range(45):
        with service.stage(run, f'阶段{index}'):
            pass
    assert len(run['stage_timings']) == 40
    assert run['stage_timings'][-1]['name'] == '阶段44'
    stored = service.repo.get('procurement_records', run['id'])
    assert stored['stage_timings'] == run['stage_timings']


def test_each_stage_of_a_real_round_is_readable_from_the_api(research):
    service, run, provider, client = research
    timings = run['stage_timings']
    assert timings, '一轮研究必须留下阶段耗时'
    # alternatives() always calls the model, so this stage carries a paid call.
    alternatives = next(entry for entry in timings if entry['name'] == '核查其他品牌')
    assert alternatives['calls']['model'] >= 1
    assert all(entry['status'] == 'completed' for entry in timings)
    assert all(entry['duration_ms'] >= 0 and entry['started_at'] <= entry['finished_at'] for entry in timings)
    # Every paid call of the round happened inside a timed stage. 每个阶段的 calls
    # 是"该阶段区间内全局花掉的次数"（进入阶段时的 usage 快照 ↔ 退出时快照），
    # 两条车道同时跑时区间重叠，同一笔调用会被两条车道各自差分到一次，所以并行下
    # 相加**可能大于**实际用量（只会多计，不会少计；少计才是"有调用游离在阶段外"）。
    # 串行时的严格等式见 tests/test_lanes_together.py 里那条关掉总开关的用例。
    assert sum(entry['calls']['model'] for entry in timings) >= run['usage']['model']
    if run.get('lanes_execution') != 'together':
        assert sum(entry['calls']['model'] for entry in timings) == run['usage']['model']
    # 两条车道现在**同时**跑（``procurement_research.LANES_TOGETHER``），所以
    # 「各阶段耗时相加」不再等于墙钟时间——重叠的那一段被算了两遍。这条断言要守的
    # 那件事仍然成立，只是要按**并集**说：这些区间合起来落在本轮墙钟之内，阶段耗时
    # 不是第二只漂走的钟。（相加口径在并行下必然大于墙钟，不是记账错。）
    from datetime import datetime
    span_ms = (datetime.fromisoformat(run['updated_at'])
               - datetime.fromisoformat(run['started_at'])).total_seconds() * 1000
    intervals = sorted((datetime.fromisoformat(entry['started_at']),
                        datetime.fromisoformat(entry['finished_at'])) for entry in timings)
    covered = 0.0
    cover_start = cover_end = None
    for start, end in intervals:
        if cover_start is None:
            cover_start, cover_end = start, end
        elif start <= cover_end:
            cover_end = max(cover_end, end)
        else:
            covered += (cover_end - cover_start).total_seconds() * 1000
            cover_start, cover_end = start, end
    covered += (cover_end - cover_start).total_seconds() * 1000
    assert covered <= span_ms + 50
    stored = service.repo.get('procurement_records', run['id'])
    assert stored['stage_timings'] == timings
    fetched = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()
    assert fetched['stage_timings'] == timings
    task = client.get(f"/api/v1/tasks/{run['task_id']}").json()['task']
    assert task['stage_timings'] == timings
    assert task['research_run_id'] == run['id']


def test_recording_timings_does_not_change_the_stage_semantics(research):
    service, run, provider, client = research
    assert run['stage'] == '结果已保存'
    assert run['status'] in {'completed', 'partial'}
    assert run['lanes']['price'] == 'completed'
    assert run['lanes']['alternatives'] == 'completed'
