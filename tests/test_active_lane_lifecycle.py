"""``active_lane`` 的生命周期：运行中表示"从哪条车道起步"，收尾后必须消失。

实测（docs/234 第 86 行，sample-d）：``finished_at`` 已写、``status=partial``、
``lanes={"price":"completed","alternatives":"pending"}`` 都对了，``active_lane`` 却在
一等 22 分钟后仍是 ``price``（见 docs/237 第 4 条）。读它就等于把一轮已经结束的研究
当成"还在跑那条车道"。正规的就绪信号是 ``status`` + ``lanes`` + ``finished_at``，
前端 ``frontend/src/services/researchProgress.js`` 也只读 ``lanes``。

两条用例分别钉住收尾的两条路：正常收尾（``_execute`` 末尾写终态）与停止收尾
（``ResearchStopped`` 分支），并顺手钉住"运行中这个字段照旧可读"。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repositories.sqlite import SQLiteRepository
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_research import ProcurementResearch, ResearchStopped

BASELINE = {'brand': '甲厂', 'model': 'Q10', 'category': '培养箱', 'scenario': '容量检测',
            'supplier_price': 6800, 'currency': 'CNY', 'quantity': 1,
            'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}]}


class _Provider:
    """离线检索通道：一条结果都不返回、也不抓正文。不碰网络。"""
    name = 'offline'

    def search(self, query):
        return []

    def fetch(self, url, timeout):
        return None


class _Model:
    def analyze(self, **kwargs):
        return {'candidates': []}


def test_a_finished_round_no_longer_claims_a_lane_is_running(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(providers=[_Provider()]))
    service = app.state.research_service
    service.client_factory = _Model
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': BASELINE['scenario']}).json()['task']['id']
        # ``baseline/save`` 是 ``confirm(..., start=False)``：run 停在 ``ready``，之后单独
        # 启动价格车道。这条就是用户点「查找已成交项目」走的路，也是 ``start_lane`` 写
        # ``active_lane`` 的唯一入口（实测出问题的 sample-d 就是这条路）。
        run = client.post(f'/api/v1/tasks/{tid}/baseline/save', json=BASELINE).json()
        rid = run['id']
        # 新建一轮本来就没有这个字段：``_execute`` 的两处读取都按缺省处理（两条车道都到期）。
        assert 'active_lane' not in run

        seen = []
        real_price_lane = service._price_lane

        def price_lane(r, b):
            # 车道真跑起来时这个字段必须还在：``_execute`` 开头的阶段名就是由它反推的
            # （``run.get('active_lane') == 'alternatives'`` 才印「核查其他品牌」）。
            seen.append(r.get('active_lane'))
            return real_price_lane(r, b)

        service._price_lane = price_lane
        started = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/start/price').json()
        assert started['active_lane'] == 'price'
        service.futures[rid].result(30)
        assert seen == ['price'], '清空只能发生在收尾那一刻，运行中的读取不受影响'

        finished = service.get(tid, rid)
        assert finished['stage'] == '结果已保存' and finished['finished_at']
        assert finished['status'] in {'completed', 'partial'}
        assert finished['lanes'] == {'price': 'completed', 'alternatives': 'pending'}
        assert 'active_lane' not in finished
        # 接口读回来的那一份也一样：脚手架就是轮询这个接口判断能不能开下一条车道。
        fetched = client.get(f'/api/v1/tasks/{tid}/research-runs/{rid}').json()
        assert 'active_lane' not in fetched


def test_a_stopped_round_also_stops_claiming_its_lane(tmp_path, monkeypatch):
    """预算/租约把这一轮停掉时同样是终态（``finished_at`` 已写），不许再留着 ``active_lane``。

    装置与 ``tests/test_research_recovery.py::test_budget_stop_reopens_lane_marked_running``
    相同：run 已经在相似品车道上，车道额度用完抛 ``ResearchStopped``。
    """
    repo = SQLiteRepository(tmp_path / 'stopped.db')
    service = ProcurementResearch(repo, None, None)
    repo.put('tasks', 'task', {'id': 'task', 'baseline_version': 1, 'procurement_status': 'running'})
    run = {'id': 'run', 'task_id': 'task', 'kind': 'research', 'status': 'running',
           'active_lane': 'alternatives', 'baseline_id': 'baseline',
           'baseline': {'version': 1, 'model': 'Q10', 'scenario': '采购', 'parameters': []},
           'created_at': '2026-01-01T00:00:00+00:00', 'product_source_checked': True,
           'lanes': {'price': 'completed', 'alternatives': 'pending'},
           'prices': [], 'alternatives': [], 'sources': [], 'gaps': [], 'completed_queries': [],
           'price_leads': [], 'usage': {'search': 0, 'fetch': 0, 'model': 0},
           'limits': {'search': 8, 'fetch': 8, 'model': 8}}
    repo.put('procurement_records', 'run', run, task_id='task')
    monkeypatch.setattr(service, 'alternatives', lambda _run: (_ for _ in ()).throw(ResearchStopped('额度已用完')))
    monkeypatch.setattr('backend.services.research_planning.discovery_queries', lambda _baseline: [])
    monkeypatch.setattr(service, 'recover_sources', lambda _run: None)
    monkeypatch.setattr(service, 'report', lambda *_args: None)
    service._execute('task', 'run')
    saved = repo.get('procurement_records', 'run')
    assert saved['status'] == 'partial' and saved['finished_at']
    assert saved['lanes'] == {'price': 'completed', 'alternatives': 'pending'}
    assert 'active_lane' not in saved
    service.close()
    repo.close()


def test_a_cancelled_round_also_stops_claiming_its_lane(tmp_path):
    """用户按下"停止"同样是终态（``finished_at`` 已写），不许再留着 ``active_lane``。

    装置与 ``tests/test_research_recovery.py::test_cancellation_records_stop_time``
    相同：run 正跑在价格车道上，``cancel`` 只改状态与停表。用户停下之后界面上的下一步
    是"继续研究"（``resume``，它自己会写 ``active_lane='all'``），所以清掉不影响恢复。
    """
    repo = SQLiteRepository(tmp_path / 'cancelled.db')
    service = ProcurementResearch(repo, None, None)
    repo.put('tasks', 'task', {'id': 'task', 'baseline_version': 1, 'procurement_status': 'running'})
    run = {'id': 'run', 'task_id': 'task', 'kind': 'research', 'status': 'running',
           'active_lane': 'price', 'baseline_id': 'baseline',
           'baseline': {'version': 1, 'model': 'Q10', 'scenario': '采购', 'parameters': []},
           'created_at': '2026-01-01T00:00:00+00:00',
           'lanes': {'price': 'running', 'alternatives': 'pending'},
           'prices': [], 'alternatives': [], 'sources': [], 'gaps': [],
           'usage': {'search': 0, 'fetch': 0, 'model': 0},
           'limits': {'search': 8, 'fetch': 8, 'model': 8}}
    repo.put('procurement_records', 'run', run, task_id='task')
    # 停之前它确实在跑那条车道：清空只发生在 ``finished_at`` 写下之后。
    assert service.get('task', 'run')['active_lane'] == 'price'
    stopped = service.cancel('task', 'run')
    assert stopped['status'] == 'cancelled' and stopped['finished_at']
    assert 'active_lane' not in stopped
    assert 'active_lane' not in repo.get('procurement_records', 'run')
    service.close()
    repo.close()
