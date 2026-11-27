"""没有「必须满足的实验要求」的一轮研究：下游逐处按真实数据走一遍。

这一组用真实的 ProcurementResearch 服务、真实的报告生成路径，不调用任何模型：
run 直接落库（确认那一刻的状态就是它的真实状态），价格记录和替代候选也直接落库，
然后读报告。离线复现，模型调用 0 次。
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_rules import (NO_CONFIRMED_PARAMETER, alternative_conclusion,
                                                comparison_details, similarity_assessment)
from backend.services.research_planning import discovery_queries, triage


class OfflineProvider:
    name = 'offline'
    reads = []
    bodies = {}

    def search(self, query):
        return []

    def fetch(self, url, timeout):
        self.reads.append(url)
        return self.bodies[url]


class OfflineModel:
    calls = 0

    def analyze(self, **kwargs):
        OfflineModel.calls += 1
        return {'candidates': []}


@pytest.fixture
def metric_less(tmp_path):
    """一个「没有确认任何指标」的真实 run，价格链照样跑完两条通道的记账。"""
    provider = OfflineProvider()
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(providers=[provider]))
    service = app.state.research_service
    service.client_factory = OfflineModel
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': '微生物培养'}).json()['task']['id']
        run = client.post(f'/api/v1/tasks/{tid}/baseline/confirm',
                          json={'brand': '甲厂', 'model': 'SPX-150BIII', 'scenario': '微生物培养',
                                'category': '生化培养箱', 'supplier_price': 6800, 'currency': 'CNY',
                                'quantity': 1, 'parameters': []}).json()
        service.futures[run['id']].result(10)
        stored = service.get(tid, run['id'])
        # 价格链该查还查：落一条真实形状的同型号成交记录。
        stored['lanes'] = {'price': 'completed', 'alternatives': 'completed'}
        stored['status'] = 'partial'
        stored['prices'] = [{
            'kind': 'award', 'brand': '甲厂', 'model': 'SPX-150BIII', 'unit_price': 6900.0,
            'amount': 6900, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
            'currency': 'CNY', 'date': '2024-05-20', 'configuration': 'unknown',
            'tax': 'unknown', 'installation': 'unknown', 'warranty': 'unknown', 'delivery': 'unknown',
            'level': 'historical_configuration', 'comparison_level': 'historical_configuration',
            'date_verified': True, 'recent': True, 'gaps': ['configuration'],
            'supplier': '某代理商', 'url': 'https://example.gov.cn/award/1',
            'excerpt': 'SPX-150BIII 成交单价6900元',
        }]
        # 替代候选：没有指标可对，矩阵就是空的——这正是要验的那种候选。
        matrix = []
        similarity = similarity_assessment(matrix, confirmed_parameter_count=0)
        candidate = {'brand': '乙厂', 'model': 'Q20', 'url': 'https://vendor.example/q20',
                     'configuration': '标准配置', 'excerpt': 'Q20 生化培养箱',
                     'matrix': matrix, **similarity,
                     'status': 'no_comparison_basis',
                     'prices': [{'kind': 'award', 'model': 'Q20', 'unit_price': 7200.0, 'currency': 'CNY',
                                 'quantity': 1, 'date': '2024-06-01', 'level': 'historical_configuration',
                                 'url': 'https://example.gov.cn/award/2'}]}
        candidate['conclusion'] = alternative_conclusion(stored['baseline'], stored['prices'], candidate)
        candidate.update(comparison_details(stored['baseline'], candidate, []))
        stored['alternatives'] = [candidate]
        # 确认那一刻已经生成过一份（没有价格的）报告，报告是存下来的、正文文字是
        # 建它那一刻算出来的。这里换了 round 让下一次读重建，才是在读**这些**价格。
        stored['round'] = 2
        service.save(stored)
        yield service, stored, client


def test_metric_less_run_records_the_state_on_confirm(metric_less):
    service, run, _ = metric_less
    assert run['comparison_basis'] == NO_CONFIRMED_PARAMETER
    assert run['confirmed_parameter_count'] == 0
    assert run['baseline']['parameters'] == []


def test_report_never_claims_compliance_without_metrics(metric_less):
    service, run, _ = metric_less
    report = service.report(run['task_id'], run['id'])
    text = json.dumps(report, ensure_ascii=False)

    # 不能说"满足全部要求"，也不能出现"全部关键指标满足"这种 0/0 的空话。
    assert '满足全部要求' not in text
    assert '全部关键指标满足' not in text
    assert '全部已列关键指标均有可核验证据' not in text
    assert '款达到70%已核验相似度' not in text
    # 要如实说出这一轮没有确认可比指标、没有做逐项参数比对。
    assert '本轮没有确认必须满足的实验要求' in text
    assert '未做逐项参数比对' in text
    # 价格与成交记录该报还报，不因为没有指标而消失（金额按报告统一口径千分位）。
    assert '6,900' in text and 'https://example.gov.cn/award/1' in text
    prices_section = next(s for s in report['sections'] if s['key'] == 'prices')
    assert prices_section['summary'].startswith('本轮列出 1 条同型号成交记录')


def test_overview_facts_do_not_turn_no_basis_into_a_match(metric_less):
    service, run, _ = metric_less
    report = service.report(run['task_id'], run['id'])
    facts = {item['label']: item['value'] for item in report['summary']['facts']}
    assert '同档替代候选' in facts
    assert '全部关键指标满足' not in facts['同档替代候选']
    assert '未做逐项参数比对' in facts['同档替代候选']


def test_alternative_section_lists_no_parameter_comparison(metric_less):
    service, run, _ = metric_less
    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s['key'] == 'alternatives')
    assert '未做逐项参数比对' in section['summary']
    # 候选本身还在（价格与来源照比），但它的结论不能是"满足"。
    assert len(section['items']) == 1
    text = json.dumps(section, ensure_ascii=False)
    assert '全部关键指标满足' not in text
    assert '未做逐项参数比对' in section['items'][0]['text']
    assert 'Q20' in section['items'][0]['label']


def test_decision_section_other_brands_line_is_honest(metric_less):
    service, run, _ = metric_less
    report = service.report(run['task_id'], run['id'])
    decision = next(s for s in report['sections'] if s['key'] == 'decision')
    line = next(item['text'] for item in decision['items'] if item['label'] == '其他品牌')
    assert '全部关键指标满足' not in line
    assert '未做逐项参数比对' in line


def test_price_decision_still_runs_without_metrics(metric_less):
    from backend.services.procurement_rules import price_decision

    service, run, _ = metric_less
    stored = service.get(run['task_id'], run['id'])
    decision = price_decision(stored['prices'], stored['baseline'], 'completed')
    # 核价判断照旧由规则引擎按价格记录给出，没有被"没有指标"影响。
    assert decision['state'] == 'historical_comparable'
    assert decision['transaction_count'] == 1
    assert '历史' in decision['summary'] or '成交' in decision['summary']
    report = service.report(run['task_id'], run['id'])
    assert report['sections'][0]['summary'] == service.get(run['task_id'], run['id'])['price_assessment']


def test_discovery_and_triage_survive_an_empty_parameter_list(metric_less):
    service, run, _ = metric_less
    baseline = run['baseline']
    queries = discovery_queries(baseline)
    # 没有数值指标，就不发按指标检索的查询；品类查询照旧，不编造指标。
    assert any('生化培养箱' in q for q in queries)
    assert not any('None' in q for q in queries)
    assert all(isinstance(q, str) and q.strip() for q in queries)
    # 没有指标可对，启发式过滤不会凭空报"低于要求"。
    assert triage({'url': 'https://a', 'model': 'Q20'}, [], baseline['parameters']) == []


def test_report_api_returns_a_zip_and_reports_endpoint_carries_the_json(tmp_path):
    """真实接口：报告生成不报错，JSON 报告经 /tasks/{id}/reports 可读。"""
    provider = OfflineProvider()
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(providers=[provider]))
    service = app.state.research_service
    service.client_factory = OfflineModel
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': '微生物培养'}).json()['task']['id']
        run = client.post(f'/api/v1/tasks/{tid}/baseline/confirm',
                          json={'brand': '甲厂', 'model': 'SPX-150BIII', 'scenario': '微生物培养',
                                'category': '生化培养箱', 'supplier_price': 6800, 'currency': 'CNY',
                                'parameters': []}).json()
        service.futures[run['id']].result(10)
        export = client.post(f'/api/v1/tasks/{tid}/research-runs/{run["id"]}/report')
        assert export.status_code == 200 and export.content.startswith(b'PK')
        items = client.get(f'/api/v1/tasks/{tid}/reports').json()['items']
        assert items, '本轮应有一份报告'
        text = json.dumps(items[0], ensure_ascii=False)
        assert '满足全部要求' not in text
        assert '全部关键指标满足' not in text
        # 结论里出现的是"没有确认必须满足的实验要求"，不是"满足"。
        assert re.search(r'本轮没有确认必须满足的实验要求', text)
