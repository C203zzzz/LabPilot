"""两条车道同时跑：机制、预算分配，以及三处改动的口径。

这一组对着 ``task-03d477711afc`` 查出来的三件事：
  ① 价格车道与相似品车道**同时**执行（设计规范要求），共享额度不被
     先跑的那条吃光；
  ② 品类缺失时发现查询不再拿整句场景话当品类；
  ③ "候选为零"的缺口文案不再把它归因给"没有实验要求"（那是产品自己写的错误归因）。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repositories.sqlite import SQLiteRepository
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_research import (LANE_RESERVE_FLAG, LaneBudgetReserved,
                                                   NO_ALTERNATIVES_NOTE, ProcurementResearch,
                                                   ResearchStopped)
from backend.services.research_planning import discovery_category, discovery_queries, discovery_sources
from tests.test_metric_optional_downstream import metric_less  # noqa: F401 - shared fixture

SAMPLE_BASELINE = {
    'brand': '湖南凯达', 'model': 'KH20A', 'category': None,
    'scenario': '采购一台 湖南凯达 KH20A 仪器，用于实验室日常检测，需要报出公开成交价与配置参数',
    'parameters': [],
}


@pytest.fixture
def lanes_service(tmp_path):
    repo = SQLiteRepository(tmp_path / 'lanes.db')
    repo.put('tasks', 'task-lanes', {'id': 'task-lanes', 'status': 'draft'})
    service = ProcurementResearch(repo, None, None)
    yield repo, service
    service.close()
    repo.close()


def _run(repo, usage=None, limits=None, baseline=None):
    run = {'id': 'research-lanes', 'task_id': 'task-lanes', 'kind': 'research',
           'created_at': '2026-01-01T00:00:00+00:00', 'status': 'running',
           'stage': '核查同款价格', 'round': 1,
           'lanes': {'price': 'running', 'alternatives': 'running'},
           'usage': dict(usage or {'search': 0, 'fetch': 0, 'model': 0}),
           'limits': dict(limits or {'search': 80, 'fetch': 160, 'model': 48}),
           'baseline': dict(baseline or {'version': 1, 'brand': '甲厂', 'model': 'Q10'}),
           'gaps': []}
    repo.put('procurement_records', run['id'], run, task_id=run['task_id'])
    return run


class _Provider:
    """离线检索通道：一条结果都不返回、正文按字典给。不碰网络。"""
    name = 'offline'

    def __init__(self, bodies=None):
        self.reads = []
        self.bodies = dict(bodies or {})

    def search(self, query):
        return []

    def fetch(self, url, timeout):
        self.reads.append(url)
        return self.bodies[url]


class _Model:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer if answer is not None else {'candidates': []}

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.answer)


def _round(tmp_path, baseline, monkeypatch=None, together=True):
    """跑完一整轮离线研究，返回 (service, 落库的 run, provider, client)。"""
    if monkeypatch is not None and not together:
        monkeypatch.setattr('backend.services.procurement_research.LANES_TOGETHER', False)
    provider = _Provider()
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(providers=[provider]))
    service = app.state.research_service
    model = _Model()
    service.client_factory = lambda: model
    client = TestClient(app)
    client.__enter__()
    tid = client.post('/api/v1/tasks', json={'scenario': baseline['scenario']}).json()['task']['id']
    run = client.post(f'/api/v1/tasks/{tid}/baseline/confirm', json=baseline).json()
    service.futures[run['id']].result(30)
    return service, service.get(tid, run['id']), provider, (model, client, app)


# --------------------------------------------------------------- ① 真的一起跑
def test_the_two_lanes_really_overlap(lanes_service):
    repo, service = lanes_service
    run = _run(repo)
    marks = {}
    claims = []

    def lane(name, seconds):
        def drive(r, b):
            claims.append(service.execution.claim)
            marks[name + '-start'] = time.perf_counter()
            time.sleep(seconds)
            marks[name + '-end'] = time.perf_counter()
        return drive

    service.round_claim = ('research:research-lanes', 'owner-1')
    service._price_lane = lane('price', 0.25)
    service._alternatives_lane = lane('alternatives', 0.25)
    started = time.perf_counter()
    service._lanes_together(run, {})
    elapsed = time.perf_counter() - started

    # 串行至少要 0.5 秒；同时跑只要一个 0.25 秒。
    assert elapsed < 0.45, elapsed
    assert marks['alternatives-start'] < marks['price-end'], '两条车道没有重叠'
    # 车道线程必须装到本轮租约：``self.execution`` 是 thread-local，不装的话
    # persist 的取消/租约检查在两条最长的阶段上就是空转。
    assert claims == [('research:research-lanes', 'owner-1')] * 2
    # 汇合后保底线关掉，后面的收尾阶段不再受它限制。
    assert LANE_RESERVE_FLAG not in run


def test_a_real_round_runs_both_lanes_together_and_their_stages_overlap(tmp_path, monkeypatch):
    service, run, _, (model, client, app) = _round(tmp_path, dict(SAMPLE_BASELINE), monkeypatch)
    try:
        assert run['lanes_execution'] == 'together'
        assert run['lanes'] == {'price': 'completed', 'alternatives': 'completed'}
        lane_stages = [e for e in run['stage_timings'] if e['name'] in {'核查同款价格', '核查其他品牌'}]
        assert {e['stage'] for e in lane_stages} == {'核查同款价格', '核查其他品牌'}, \
            '并行时每个阶段条目仍旧自带本车道的名字，不能是合起来的那一个词'
        assert len(lane_stages) == 2
        intervals = sorted((datetime.fromisoformat(e['started_at']),
                            datetime.fromisoformat(e['finished_at'])) for e in lane_stages)
        assert intervals[0][1] >= intervals[1][0], '两条车道的阶段区间没有重叠，说明并没有同时跑'
        assert run['stage'] == '结果已保存'
    finally:
        client.__exit__(None, None, None)


def test_the_kill_switch_falls_back_to_the_serial_order(tmp_path, monkeypatch):
    service, run, _, (model, client, app) = _round(tmp_path, dict(SAMPLE_BASELINE), monkeypatch,
                                                  together=False)
    try:
        assert run['lanes_execution'] == 'serial'
        assert run['lanes'] == {'price': 'completed', 'alternatives': 'completed'}
        lane_stages = [e for e in run['stage_timings'] if e['name'] in {'核查同款价格', '核查其他品牌'}]
        intervals = sorted((datetime.fromisoformat(e['started_at']),
                            datetime.fromisoformat(e['finished_at'])) for e in lane_stages)
        assert intervals[0][1] <= intervals[1][0], '串行时价格车道必须先跑完'
        # 串行时每个阶段的 calls 是互不重叠的区间差分，相加必须正好等于实际用量：
        # 这条等式才是"没有一笔付费调用游离在阶段之外"的严格守门人。并行下它不成立
        # （区间重叠会重复计数），所以并行的弱口径写在 test_research_stage_timings.py。
        for key in ('search', 'fetch', 'model'):
            assert sum(e['calls'][key] for e in run['stage_timings']) == run['usage'][key], key
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------------- ① 预算不被花超/不饿死
def test_two_threads_cannot_overspend_the_shared_budget(lanes_service):
    repo, service = lanes_service
    run = _run(repo, limits={'search': 6, 'fetch': 10, 'model': 4})

    def spend(_):
        try:
            service.consume(run, 'search')
            return True
        except ResearchStopped:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = list(pool.map(spend, range(40)))
    # 先比较再自增必须原子：不加锁时多个线程会一起通过检查，usage 冲破 limits。
    assert sum(granted) == 6
    assert run['usage']['search'] == 6


def test_the_price_lane_stops_at_the_reserve_and_the_similar_lane_does_not(lanes_service):
    repo, service = lanes_service
    run = _run(repo, limits={'search': 10, 'fetch': 10, 'model': 4})
    run[LANE_RESERVE_FLAG] = True
    service.spending_lane.name = 'price'
    # search 保底 8：价格车道最多花到 2（再花一次就是 2+8 == 10，该停）。
    service.consume(run, 'search')
    service.consume(run, 'search')
    assert run['usage']['search'] == 2
    with pytest.raises(LaneBudgetReserved):
        service.consume(run, 'search')
    assert run['usage']['search'] == 2, '撞到保底线的那一次不能算花掉'
    # 相似品车道花的是留给它的那一份，不受保底线限制。
    service.spending_lane.name = 'alternatives'
    for _ in range(3):
        service.consume(run, 'search')
    assert run['usage']['search'] == 5
    # 保底线只在并行时生效：旗标一撤，价格车道照旧能花到硬上限。
    run.pop(LANE_RESERVE_FLAG)
    service.spending_lane.name = 'price'
    while run['usage']['search'] < 10:
        service.consume(run, 'search')
    with pytest.raises(ResearchStopped) as excinfo:
        service.consume(run, 'search')
    assert not isinstance(excinfo.value, LaneBudgetReserved)


def test_the_price_lane_turns_pending_instead_of_killing_the_round(lanes_service, monkeypatch):
    """价格车道撞保底线：它自己停下并标回 pending，整轮不走"已停止"。"""
    repo, service = lanes_service
    run = _run(repo, limits={'search': 10, 'fetch': 10, 'model': 4},
               baseline={'version': 1, 'brand': '甲厂', 'model': 'Q10', 'category': '离心机',
                         'parameters': []})

    def blow_up(_run, _text, _purpose, target=None):
        raise LaneBudgetReserved('价格核查已花到为相似品核查预留的额度线（search 还剩 2 次），先停下。')

    monkeypatch.setattr(service, 'query', blow_up)
    service._price_lane(run, run['baseline'])          # 必须不抛出
    assert run['lanes']['price'] == 'pending'
    assert any('预留额度' in g for g in run['gaps']), run['gaps']
    assert run['status'] == 'running', '整轮没有被这一条车道的保底线判成停止'


def test_stage_entries_from_two_lanes_both_survive(lanes_service):
    repo, service = lanes_service
    run = _run(repo)
    started, clock = '2026-01-01T00:00:00+00:00', time.perf_counter()

    def record(index):
        for _ in range(10):
            service.record_stage(run, '阶段%d' % index, started, clock, {}, 'completed')

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, (0, 1)))
    # 读出—追加—写回不加锁时后写的那份会盖掉前一份，条目凭空少掉。
    assert len(run['stage_timings']) == 20


def test_the_aggregator_fallback_is_paid_once_when_both_lanes_ask(lanes_service, monkeypatch):
    repo, service = lanes_service
    run = _run(repo, baseline={'version': 1, 'brand': '甲厂', 'model': 'Q10'})
    run['lanes'] = {'price': 'completed', 'alternatives': 'running'}
    run['prices'] = []
    paid = []

    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, model):
            paid.append(model)
            time.sleep(0.05)          # 把竞态窗口拉开：没有闸门锁时两次都会进去
            return SimpleNamespace(leads=[], channels={}, partial_reason='')

        def official_recheck(self, leads, searcher=None, limit=0):
            return SimpleNamespace(searched=0, verified=[], gaps=[])

    monkeypatch.setattr('backend.services.aggregator_discovery.AggregatorDiscovery', FakeDiscovery)
    monkeypatch.setattr(service, 'aggregator_transport', lambda: (None, None))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.aggregator_leads(run, price_lane_finished=True), range(2)))
    # 闸门是"先查留痕再花钱"：查与写不原子时两个车道会同时通过，同一轮花两次钱。
    assert len(paid) == 1, paid


# ----------------------------------------------------------- ② 品类缺失的检索式
def test_a_missing_category_no_longer_makes_the_whole_scenario_the_category():
    queries = discovery_queries(SAMPLE_BASELINE)
    assert queries, '品类缺失也要发检索式，只是不再拿场景话当品类'
    # 这条样本的原样：三条查询每条都带整句场景话，抓回来的 7 篇发现正文里 5 篇是
    # 原型号自己的销售页/参数页。
    assert not any(SAMPLE_BASELINE['scenario'][:40] in q for q in queries)
    # 品类未知时"品类 + 技术参数"这类检索式成立不了：去掉品类，唯一的区分词只剩
    # 型号，捞回来的必然是本品型号自己的产品页——那种正文里不可能有别的品牌。
    assert not any('技术参数' in q for q in queries)
    # 两条都指向印着竞品型号的采购公告语料，且都排除原品牌。
    assert len(queries) == 2
    assert all('中标' in q for q in queries)
    assert all('-"湖南凯达"' in q for q in queries)
    # 两条都要锚住型号：一条用原样写法，一条用厂商常写的空格写法。
    assert all(('KH20A' in q) or ('KH 20A' in q) for q in queries)
    assert any('KH20A' in q for q in queries)
    assert any('KH 20A' in q for q in queries)


def test_a_category_inferred_from_the_scenario_is_used_instead_of_the_prose():
    baseline = {'brand': '甲厂', 'model': 'Q10', 'category': None,
                'scenario': '采购一台生物安全柜用于微生物检测', 'parameters': []}
    assert discovery_category(baseline) == '生物安全柜'
    queries = discovery_queries(baseline)
    assert all('生物安全柜' in q for q in queries)
    assert not any('采购一台' in q for q in queries)


def test_a_confirmed_category_keeps_the_existing_query_shape():
    baseline = {'brand': 'Original', 'category': '恒温水浴', 'model': 'B12',
                'parameters': [{'name': '容量', 'value': 14, 'unit': 'L', 'direction': 'min'}]}
    queries = discovery_queries(baseline)
    assert len(queries) == 3
    assert queries[0].startswith('恒温水浴 容量')
    assert '中标' in queries[-1] and '报价明细' in queries[-1]
    assert all('-"Original"' in q for q in queries)


# ------------------------------------------- ④ 窗口不能被采购公告占满（另一单的病根）
def _source(url, purpose, source_type):
    return {'url': url, 'purpose': purpose, 'source_type': source_type,
            'body_status': 'fetched', 'body_text': 'x'}
# ------------------------------------------------------ ③ 缺口文案不再错误归因
def test_the_no_candidate_gap_no_longer_blames_the_missing_requirements(tmp_path, monkeypatch):
    service, run, _, (model, client, app) = _round(tmp_path, dict(SAMPLE_BASELINE), monkeypatch)
    try:
        assert run['comparison_basis'] == 'no_confirmed_parameter'
        assert not run['discovered_candidates'] and not run['alternatives']
        assert any('没有从已取得的正文中识别出其他品牌的候选' in g for g in run['gaps']), run['gaps']
        # 从前那一句把两件事写成因果；发现阶段不看 parameters，所以"补上实验要求就会
        # 有候选"是假的。
        assert not any('因此没有可比对的跨品牌候选' in g for g in run['gaps']), run['gaps']
    finally:
        client.__exit__(None, None, None)


def test_the_no_candidate_section_stops_promising_rows_it_does_not_have(metric_less):  # noqa: F811
    service, run, _ = metric_less
    stored = service.get(run['task_id'], run['id'])
    stored['alternatives'] = []
    stored['discovered_candidates'] = []
    stored['reference_candidates'] = []
    stored['round'] = stored['round'] + 1
    service.save(stored)
    report = service.report(stored['task_id'], stored['id'])
    section = next(s for s in report['sections'] if s['key'] == 'alternatives')
    assert section['items'] == []
    # 空节的引导语只有一处出口（``apply_empty_section_notes`` ＋ ``NO_ALTERNATIVES_NOTE``），
    # 新建的报告与读回来的旧报告都落到同一句上，所以这里按**整句**比对。合并前两单各写了
    # 一句同义的话（210 那句在报告里被这一处覆盖、到不了读者眼前），只按各自那句断言就会
    # 时同时对、时而不对。
    assert section['summary'] == NO_ALTERNATIVES_NOTE, section['summary']
    # 前端在同样情形下印的是同一句（``ResearchResults.jsx``）；报告从前没有这句，
    # 读起来像市场上没有替代品。
    assert '不能据此认定市场上没有替代品' in section['summary']
    assert '这里只列价格与来源' not in section['summary']


def test_the_price_gap_does_not_read_as_no_deal_exists_anywhere(tmp_path):
    """零成交的缺口文案必须带上前端那句限定，不能读成"市场上查不到成交"。"""
    service, run, _, (model, client, app) = _round(tmp_path, dict(SAMPLE_BASELINE))
    try:
        gap = next(g for g in run['gaps'] if '未取得可核实的同型号成交单价' in g)
        assert '不等于这台机器在别处没有成交记录' in gap, gap
    finally:
        client.__exit__(None, None, None)


def test_the_report_gap_section_speaks_the_same_language_as_the_overview(metric_less):  # noqa: F811
    """同一份报告里，总述与缺口节不能一个说人话、一个印机器码。"""
    service, run, _ = metric_less
    stored = service.get(run['task_id'], run['id'])
    stored['gaps'] = ['empty_results', 'no_official_source', 'empty_results']
    stored['round'] = stored['round'] + 1
    service.save(stored)
    report = service.report(stored['task_id'], stored['id'])
    section = next(s for s in report['sections'] if s['key'] == 'gaps')
    texts = [item['text'] for item in section['items']]
    assert not any('empty_results' in t or 'no_official_source' in t for t in texts), texts
    # 去重发生在换成人话之后，所以同一句不会印两遍。
    assert len(texts) == len(set(texts))
    assert '这一次检索没有返回任何结果' in texts


def test_candidates_are_still_listed_without_parameters(metric_less):  # noqa: F811
    """守住"无参数也要列出候选"这个设计意图：有候选时那一节照旧列出来。"""
    service, run, _ = metric_less
    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s['key'] == 'alternatives')
    assert len(section['items']) == 1
    assert '这里只列价格与来源' in section['summary']
    assert 'Q20' in section['items'][0]['label']
    assert '未做逐项参数比对' in section['items'][0]['text']
    assert '全部关键指标满足' not in json.dumps(section, ensure_ascii=False)
