# -*- coding: utf-8 -*-
"""沉淀知识库（K1~K5）的存取契约。

这一层只做存取，**不重新裁决任何事**：所以测试钉的是"记了什么、取回什么、
没记进去的有没有留痕"，而不是价格或配置判据（那些在 ``test_price_rejections.py``
等文件里）。K1~K5 的定义见 ``回归基线`` 阶段四。
"""
import logging
import sqlite3
from types import SimpleNamespace

from backend.services import knowledge_memory

SOURCE_URL = 'https://gdegp.example.gov.cn/award/1'
REJECT_URL = 'https://ccgp.example.gov.cn/zbgg/2.htm'
SKIP_URL = 'https://download.example.gov.cn/oss/download?uuid=1'
DEAD_URL = 'https://www.example-aggregator.com/productview.asp?id=4827'


def _run(**overrides):
    run = {
        'id': 'research-1',
        'task_id': 'task-1',
        'created_at': '2026-10-06T16:14:00+00:00',
        'finished_at': '2026-10-06T16:20:00+00:00',
        'baseline': {
            'model': 'HVE-50', 'brand': 'HIRAYAMA', 'category': '灭菌器',
            'parameters': [
                {'name': '有效容积', 'value': 50, 'unit': '升', 'direction': 'equal'},
                {'name': '灭菌温度', 'value': 105, 'unit': '℃', 'direction': 'equal'},
            ],
        },
        'sources': [{
            'url': SOURCE_URL, 'body_status': 'fetched', 'source_type': 'award',
            'body_text': '型号 HVE50 与 HVE-50 各一台，品牌写的是 Hirayama。',
            'snapshot': {'sha256': 'abc123'}, 'fetched_at': '2026-10-06T16:15:00+00:00',
        }],
        'prices': [{
            'model': 'HVE-50', 'brand': 'Hirayama', 'unit_price': 62000.0, 'currency': 'CNY',
            'quantity': 1.0, 'date': '2026-02-04', 'url': SOURCE_URL, 'kind': 'award',
            'source_type': 'award', 'comparison_level': 'historical_configuration',
            'configuration_url': SOURCE_URL, 'configuration_source': 'product_page_spec',
            'excerpt': 'HIRAYAMA | HVE-50 | 62000.00',
        }],
        'price_rejections': [{
            'url': REJECT_URL, 'model': 'HVE-50', 'reason_code': 'brand_mismatch',
            'other_brand': '艾贝泰',
            'detail': '引文中的品牌是 艾贝泰，与目标品牌 HIRAYAMA 不一致。',
            'excerpt': '艾贝泰 HVE-50 68000', 'snapshot_sha256': 'def456',
        }],
        'price_source_skips': [{
            'url': SKIP_URL, 'host': 'download.example.gov.cn', 'target_model': 'HVE-50',
            'skip_reason': 'no_amount_evidence', 'body_len': 1200,
            'source_type': 'tender_document',
        }],
        'unavailable_sources': [{
            'url': DEAD_URL, 'reason': '该来源连续两次无法读取（来源正文为空）。'}],
        'alternatives': [{
            'model': 'BKQ-B75II', 'brand': '新华', 'configuration': '容积 75L，设计温度 138℃',
            'configuration_url': 'https://maker.example.com/bkq',
            'configuration_source': 'product_page_spec'}],
        'discovered_candidates': [],
        'reference_candidates': [],
    }
    run.update(overrides)
    return run


def _db(tmp_path):
    return tmp_path / 'knowledge.sqlite3'


# --------------------------------------------------------------------------- #
# K1 成交价证据
# --------------------------------------------------------------------------- #

def test_k1_stores_the_accepted_price_with_scope_snapshot_and_reason(tmp_path):
    database = _db(tmp_path)
    summary = knowledge_memory.record_run(_run(), database_path=database)
    assert summary['written']['K1'] == 2  # 一条采信 + 一条拒收

    got = knowledge_memory.recall_model('HVE-50', database_path=database)['price_evidence']
    accepted = [row for row in got if row['verdict'] == 'accepted']
    assert len(accepted) == 1
    row = accepted[0]
    assert row['amount'] == 62000.0
    assert row['deal_date'] == '2026-02-04'
    assert row['scope'] == 'historical_configuration'
    assert row['url'] == SOURCE_URL
    assert row['snapshot_sha256'] == 'abc123'      # 价格行没有哈希，从来源快照补上
    assert row['kind'] == 'award'
    assert row['runs'] == ['research-1']
    assert row['first_run_id'] == 'research-1'


def test_k1_stores_the_rejected_price_with_its_reason(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    got = knowledge_memory.recall_model('HVE-50', database_path=database)['price_evidence']
    rejected = [row for row in got if row['verdict'] == 'rejected']
    assert len(rejected) == 1
    row = rejected[0]
    assert row['reason_code'] == 'brand_mismatch'
    assert '艾贝泰' in row['reason_text']
    assert row['other_brand'] == '艾贝泰'
    # 品牌键取的是**本轮目标品牌**：这条证据是针对这台机器的，不然下次按品牌取不回。
    assert row['brand'] == 'HIRAYAMA'
    assert row['snapshot_sha256'] == 'def456'
    assert row['amount'] is None


def test_k1_recall_does_not_widen_the_brand(tmp_path):
    """品牌对不上就取不到：记忆库没有资格替品牌闸做主（与 ``price_memory`` 同向）。"""
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    assert knowledge_memory.recall_model('HVE-50', '别的品牌',
                                         database_path=database)['price_evidence'] == []
    assert knowledge_memory.recall_model('HVE-50', 'HIRAYAMA',
                                         database_path=database)['price_evidence']


# --------------------------------------------------------------------------- #
# K2 配置出处
# --------------------------------------------------------------------------- #

def test_k2_stores_which_page_carries_the_clean_configuration(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    rows = knowledge_memory.recall_model('BKQ-B75II',
                                         database_path=database)['configuration_evidence']
    assert len(rows) == 1
    assert rows[0]['url'] == 'https://maker.example.com/bkq'
    assert rows[0]['source'] == 'product_page_spec'
    assert '容积 75L' in rows[0]['excerpt']
    # 那一页本轮没抓过，抽取时间就空着——不许拿本轮时间冒充那一页的抓取时间。
    assert rows[0]['extracted_at'] == ''


def test_k2_uses_the_source_fetch_time_when_the_page_is_known(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    rows = knowledge_memory.recall_model('HVE-50',
                                         database_path=database)['configuration_evidence']
    assert [row['url'] for row in rows] == [SOURCE_URL]
    assert rows[0]['extracted_at'] == '2026-10-06T16:15:00+00:00'


def test_k2_does_not_call_a_candidate_without_configuration_a_skip(tmp_path):
    """一条既没网址、又没来源标记、又没配置文本的候选不是"跳过了一条素材"：
    把它记成跳过只会把真实的跳过淹掉（实测一份台账 5 轮能虚增 91 条）。"""
    database = _db(tmp_path)
    run = _run()
    run['discovered_candidates'] = [{'model': 'VS-840K', 'brand': '苏泰'}]
    summary = knowledge_memory.record_run(run, database_path=database)
    assert summary['skipped'] == {}
    assert summary['notes'] == []


# --------------------------------------------------------------------------- #
# K3 型号与品牌写法
# --------------------------------------------------------------------------- #

def test_k3_stores_the_spellings_seen_in_notices_and_pages(tmp_path):
    from backend.services.procurement_rules import identity
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    rows = knowledge_memory.recall_model('HVE-50', database_path=database)['model_spellings']
    model_spellings = {row['spelling'] for row in rows if row['spelling_kind'] == 'model'}
    # 正文里逐字写着的两种写法都要收到（用的是既有那把型号尺子，不放宽）。
    assert 'HVE-50' in model_spellings
    assert len(model_spellings) >= 2
    assert all(identity(spelling) == identity('HVE-50') for spelling in model_spellings)
    brand_spellings = {row['spelling'] for row in rows if row['spelling_kind'] == 'brand'}
    assert 'Hirayama' in brand_spellings
    assert all(row['model_key'] == identity('HVE-50') for row in rows)


def test_k3_does_not_pull_in_unrelated_models(tmp_path):
    """正文里出现的无关型号不许挂到这台机器头上（本轮关心的型号只有基准与候选）。"""
    database = _db(tmp_path)
    run = _run()
    run['sources'][0]['body_text'] += ' 另有 FTIR-850 一台。'
    knowledge_memory.record_run(run, database_path=database)
    rows = knowledge_memory.recall_model('HVE-50', database_path=database)['model_spellings']
    assert 'FTIR-850' not in {row['spelling'] for row in rows}


# --------------------------------------------------------------------------- #
# K4 已知不合格来源
# --------------------------------------------------------------------------- #

def test_k4_stores_every_named_bad_source_with_its_reason(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    rows = knowledge_memory.recall_model('HVE-50', database_path=database)['bad_sources']
    by_url = {row['url']: row for row in rows}
    assert set(by_url) == {REJECT_URL, SKIP_URL, DEAD_URL}
    assert by_url[REJECT_URL]['reason_code'] == 'brand_mismatch'
    assert by_url[SKIP_URL]['reason_code'] == 'no_amount_evidence'
    assert by_url[SKIP_URL]['host'] == 'download.example.gov.cn'
    assert by_url[DEAD_URL]['reason_code'] == 'unreadable'


# --------------------------------------------------------------------------- #
# K5 品类参数与展示顺序
# --------------------------------------------------------------------------- #

def test_k5_stores_the_parameter_order_the_model_decided(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), category='灭菌器', database_path=database)
    rows = knowledge_memory.recall_category('灭菌器', database_path=database)['parameters']
    assert [row['parameter_name'] for row in rows] == ['有效容积', '灭菌温度']
    assert [row['position'] for row in rows] == [1, 2]


def test_k5_follows_the_latest_run_order_and_counts_the_runs(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), category='灭菌器', database_path=database)
    second = _run(id='research-2')
    second['baseline']['parameters'] = [
        {'name': '灭菌温度', 'value': 105, 'unit': '℃', 'direction': 'equal'},
        {'name': '内腔材料', 'value': 'SUS304', 'unit': '', 'direction': 'equal'},
        {'name': '有效容积', 'value': 50, 'unit': '升', 'direction': 'equal'},
    ]
    knowledge_memory.record_run(second, category='灭菌器', database_path=database)
    rows = knowledge_memory.recall_category('灭菌器', database_path=database)['parameters']
    by_name = {row['parameter_name']: row for row in rows}
    assert by_name['灭菌温度']['position'] == 1          # 最近一轮的位置覆盖首次那个
    assert by_name['灭菌温度']['runs'] == ['research-1', 'research-2']
    assert by_name['内腔材料']['times_seen'] == 1
    # 见过两次的排前面，同次数按最近一轮的展示次序。
    assert [row['parameter_name'] for row in rows][:2] == ['灭菌温度', '有效容积']


def test_k5_uses_the_baseline_category_when_none_is_passed(tmp_path):
    database = _db(tmp_path)
    knowledge_memory.record_run(_run(), database_path=database)
    assert knowledge_memory.recall_category('灭菌器', database_path=database)['parameters']


# --------------------------------------------------------------------------- #
# 幂等 / 跳过留痕 / 开关
# --------------------------------------------------------------------------- #

def test_ingesting_the_same_ledger_twice_does_not_double_anything(tmp_path):
    database = _db(tmp_path)
    first = knowledge_memory.record_run(_run(), category='灭菌器', database_path=database)
    counts_before = knowledge_memory.counts(database_path=database)
    second = knowledge_memory.record_run(_run(), category='灭菌器', database_path=database)

    assert first['written'] == counts_before
    assert sum(second['written'].values()) == 0
    assert second['already_stored'] > 0
    assert knowledge_memory.counts(database_path=database) == counts_before
    # 行内内容也没变：见过次数不因为重复灌而虚高，轮次还是那一轮。
    rows = knowledge_memory.recall_category('灭菌器', database_path=database)['parameters']
    assert all(row['times_seen'] == 1 for row in rows)
    assert all(row['runs'] == ['research-1'] for row in rows)


def test_skips_are_recorded_one_by_one_with_a_reason(tmp_path):
    """跳过 ≠ 丢数据：每一条没写进去的记录都留一句"为什么"。

    计数口径是**按节·条**：同一条没地址的拒收记录会在 K1（价格证据）与 K4
    （不合格来源）各跳一次，算两条——两节各自少了一条素材，报一条就少报一节。
    """
    database = _db(tmp_path)
    run = _run()
    run['baseline']['parameters'] = []                      # K5 没有素材
    run['baseline']['category'] = ''                        # 品类也空
    run['price_rejections'] = [{'model': 'HVE-50', 'reason_code': 'model_mismatch',
                                'detail': '型号对不上'}]     # 这条没有来源地址
    summary = knowledge_memory.record_run(run, database_path=database)

    assert summary['skipped']['no_url'] == 2
    assert summary['skipped']['no_category'] == 1
    reasons = {note['reason_code'] for note in summary['notes']}
    assert {'no_url', 'no_category'} <= reasons
    assert {note['section'] for note in summary['notes'] if note['reason_code'] == 'no_url'} \
        == {'K1', 'K4'}
    connection = sqlite3.connect(database)
    try:
        recorded = connection.execute(
            "SELECT count(*) FROM ingest_notes WHERE outcome = 'skipped'").fetchone()[0]
        logged = connection.execute('SELECT count(*) FROM ingest_log').fetchone()[0]
    finally:
        connection.close()
    assert recorded == len(summary['notes']) == 3
    assert logged == 1


def test_switching_the_memory_off_writes_nothing(tmp_path, monkeypatch):
    database = _db(tmp_path)
    monkeypatch.setenv('LAB_KNOWLEDGE_MEMORY', 'off')
    summary = knowledge_memory.record_run(_run(), database_path=database)
    assert sum(summary['written'].values()) == 0
    assert not database.exists()
    assert knowledge_memory.recall_model('HVE-50', database_path=database)['price_evidence'] == []
    assert knowledge_memory.counts(database_path=database) == {
        'K1': 0, 'K2': 0, 'K3': 0, 'K4': 0, 'K5': 0}


def test_a_missing_database_is_read_as_no_memory(tmp_path):
    """库不存在＝还没有沉淀，不是错误（第一次研究同型号走的就是这条路）。"""
    got = knowledge_memory.recall({'model': 'HVE-50', 'brand': 'HIRAYAMA', 'category': '灭菌器'},
                                  database_path=tmp_path / 'never-created.sqlite3')
    assert got['model']['price_evidence'] == []
    assert got['category']['parameters'] == []


# --------------------------------------------------------------------------- #
# 接到研究收尾
# --------------------------------------------------------------------------- #

class _Repo:
    """最小仓储替身：留痕走 ``merge_procurement_record``（只改给到的键）。"""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.merged = []

    def list(self, kind, task_id):
        return []

    def get(self, kind, key):
        return dict(self.rows.get(key) or {})

    def merge_procurement_record(self, run_id, fields):
        row = dict(self.rows.get(run_id) or {})
        row.update(fields)
        self.rows[run_id] = row
        self.merged.append(row)
        return row


def _service(repo=None):
    """一个最小的研究服务替身：``remember_knowledge`` 只用到 repo 与内存里的 run。"""
    from backend.services.procurement_research import ProcurementResearch
    service = SimpleNamespace(repo=repo or _Repo())
    service._remember_knowledge = lambda task_id, run: ProcurementResearch._remember_knowledge(
        service, task_id, run)
    service._trace_knowledge = lambda run, **fields: ProcurementResearch._trace_knowledge(
        service, run, **fields)
    return service


def test_remember_knowledge_runs_at_the_end_of_a_round_and_leaves_a_trace(tmp_path):
    from backend.services.procurement_research import ProcurementResearch
    service = _service()
    ProcurementResearch.remember_knowledge(service, 'task-1', _run())
    run = service.repo.merged[-1]
    assert run['knowledge_memory']['status'] == 'stored'
    assert run['knowledge_memory']['written']['K1'] == 2
    assert run['knowledge_memory_recall']['status'] == 'read'
    assert run['knowledge_memory_recall']['price_evidence'] == 2


def test_remember_knowledge_reads_back_what_earlier_rounds_left(tmp_path):
    """第二次研究同型号：取回的是**上一轮**留下的沉淀，条数记在 run 上。"""
    from backend.services.procurement_research import ProcurementResearch
    knowledge_memory.record_run(_run(), category='灭菌器')
    service = _service()
    ProcurementResearch.remember_knowledge(service, 'task-1', _run(id='research-2'))
    recall = service.repo.merged[-1]['knowledge_memory_recall']
    assert recall['price_evidence'] == 2
    assert recall['category_parameters'] == 2
    assert recall['configuration_evidence'] == 1
    trace = service.repo.merged[-1]['knowledge_memory']
    # 第二轮见到的都是上一轮存过的：行不新增，只把轮次记上，库内条数不变。
    assert sum(trace['written'].values()) == 0
    assert trace['updated']['K1'] == 2
    assert knowledge_memory.counts()['K1'] == 2
    accepted = [row for row in knowledge_memory.recall_model('HVE-50')['price_evidence']
                if row['verdict'] == 'accepted']
    assert accepted[0]['runs'] == ['research-1', 'research-2']
    assert accepted[0]['first_run_id'] == 'research-1'


def test_a_failing_store_never_breaks_the_research_round(tmp_path):
    from backend.services.procurement_research import ProcurementResearch

    class DeadRepo(_Repo):
        def merge_procurement_record(self, run_id, fields):
            raise sqlite3.OperationalError('disk I/O error')

    run = _run()
    service = _service(DeadRepo())
    ProcurementResearch.remember_knowledge(service, 'task-1', run)   # 不抛
    # 落库失败了，但"失败"这件事本身仍然留在这一轮的 run 上。
    assert run['knowledge_memory']['status'] == 'failed'


def test_a_double_failure_still_leaves_a_redacted_line_in_the_log(caplog):
    """写库与留痕**同时**失败：库与 run 都留不下，至少要在日志里留一条脱敏的记录。

    ``设计规范`` §2d 第三条：原来的内层 ``except Exception: return`` 把这一格**完全吞掉**
    ——库没有记录、日志 0 条，痕迹只在内存 run 上、进程结束即消失，与 ``设计规范`` §5
    「失败要留脱敏后的 error 原文」在半截上冲突。修后：落不了库，就落到日志。
    """
    from backend.services.procurement_research import ProcurementResearch

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError('disk I/O error')

    service = _service()
    service._remember_knowledge = boom
    service._trace_knowledge = lambda run, **fields: (_ for _ in ()).throw(
        sqlite3.OperationalError('database is locked'))
    run = _run()
    with caplog.at_level(logging.WARNING, logger='backend.services.procurement_research'):
        ProcurementResearch.remember_knowledge(service, 'task-1', run)   # 不抛
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'disk I/O error' in text and 'database is locked' in text
    assert '沉淀' in text


def test_a_cancelled_round_writes_no_memory_and_keeps_the_cancelled_status():
    """取消的轮次没有终态成果：不写沉淀，也不再落库（不许盖掉 cancelled）。

    M8 把收尾沉淀挂在整个 ``try/except`` 之后，而 ``except ResearchStopped``
    判定到 ``cancelled`` 时既不改状态也不 ``return``，于是取消的轮次也会写沉淀，
    并把内存里仍停在 ``running`` 的整份 run 写回库——库里的 ``cancelled`` 被盖成
    ``running``，下一次读取就按"无租约的孤儿轮次"reconcile 成 ``interrupted``
    （``tests/test_procurement_workflow.py::test_cancellation_does_not_get_overwritten``
    与 ``设计规范``）。
    """
    from backend.services.procurement_research import ProcurementResearch, ResearchStopped

    stored = {'id': 'research-1', 'task_id': 'task-1', 'status': 'cancelled',
              'baseline': {'model': 'HVE-50', 'brand': 'HIRAYAMA'}, 'lanes': {}}
    calls = []

    def persist(run):
        raise ResearchStopped('已取消')

    service = SimpleNamespace(
        execution=SimpleNamespace(),          # 没有 claim：不是租约失主那条路
        persist=persist,
        get=lambda task_id, run_id: dict(stored),   # 库里那一行已经由取消接口写成终态
        save=lambda run: calls.append(('save', run)),
        remember_knowledge=lambda task_id, run: calls.append(('memory', run)))
    ProcurementResearch._execute(service, 'task-1', 'research-1')
    assert calls == []
    assert stored['status'] == 'cancelled'


def test_the_memory_trace_never_covers_the_status_another_path_just_wrote(tmp_path):
    """留痕只合并自己那两个键：内存里落后的 ``running`` 不许盖掉库里的终态。"""
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.procurement_research import ProcurementResearch

    repo = SQLiteRepository(tmp_path / 'trace.db')
    repo.put('procurement_records', 'research-1',
             {'id': 'research-1', 'task_id': 'task-1', 'kind': 'research',
              'status': 'cancelled', 'baseline': {'model': 'HVE-50', 'version': 1}},
             task_id='task-1')
    service = SimpleNamespace(repo=repo)
    stale = {'id': 'research-1', 'task_id': 'task-1', 'status': 'running'}
    ProcurementResearch._trace_knowledge(service, stale, knowledge_memory={'status': 'stored'})
    after = repo.get('procurement_records', 'research-1')
    assert after['status'] == 'cancelled'
    assert after['knowledge_memory'] == {'status': 'stored'}
