import threading
from datetime import date

from fastapi.testclient import TestClient
from backend.main import create_app
from backend.services.official_search import OfficialSearchService, FakeSearchProvider
from backend.services.procurement_rules import validate_price, evaluate_parameter, contains_model


def test_price_requires_item_identity_and_rejects_package():
    baseline = {'model': 'X-100', 'brand': '厂商', 'supplier_price': 12000}
    text = '中标明细 厂商 X-100 数量2台 明细总价2万元'
    source = {'url': 'https://example.gov.cn/award', 'body_status': 'fetched', 'body_text': text, 'source_type': 'tender_document'}
    row = {'model': 'X-100', 'brand': '厂商', 'kind': 'award', 'amount': 2, 'amount_unit': '万元', 'quantity': 2,
           'amount_scope': 'single_item_total', 'excerpt': text, 'date': date.today().isoformat()}
    result = validate_price(row, source, baseline)
    assert result['unit_price'] == 10000
    assert result['delta'] is None and result['level'] == 'model_reference'
    assert validate_price({**row, 'amount_scope': 'package_total'}, source, baseline) is None
    assert validate_price({**row, 'model': 'X-100S'}, source, baseline) is None
    assert validate_price({**row, 'amount': 1}, source, baseline) is None
    assert validate_price(row, {**source, 'body_status': 'failed'}, baseline) is None
    assert not contains_model('X-100S', 'X-100')


def test_same_brand_complete_model_unit_price_and_date_is_historical_comparable_with_gaps():
    body = '成交公告 2025年6月18日 厂商 X-100 单价6800元 数量2台'
    source = {'url': 'https://example.gov.cn/award-x100', 'body_status': 'fetched',
              'body_text': body, 'source_type': 'tender_document'}
    raw = {'kind': 'award', 'brand': '厂商', 'model': 'X-100', 'amount': 6800,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 2,
           'excerpt': body, 'date': '2025-06-18', 'currency': 'CNY'}
    result = validate_price(raw, source, {'brand': '厂商', 'model': 'X-100',
                                          'quantity': 1, 'currency': 'CNY'})
    assert result['level'] == 'historical_configuration'
    assert result['unit_price'] == 6800
    assert set(result['gaps']) >= {'configuration', 'tax', 'installation', 'warranty', 'delivery', 'quantity'}
    assert result['delta'] is None


def test_missing_brand_does_not_pass_historical_comparability_gate():
    body = '成交公告 2025年6月18日 X-100 单价6800元 数量1台'
    source = {'url': 'https://example.gov.cn/award-no-brand', 'body_status': 'fetched',
              'body_text': body, 'source_type': 'tender_document'}
    raw = {'kind': 'award', 'model': 'X-100', 'amount': 6800, 'amount_unit': '元',
           'amount_scope': 'unit', 'quantity': 1, 'excerpt': body, 'date': '2025-06-18'}
    result = validate_price(raw, source, {'brand': '厂商', 'model': 'X-100'})
    assert result['level'] == 'model_reference'
    assert result['delta'] is None


def test_source_publication_date_can_ground_table_price_without_row_date():
    body = '成交公告 厂商 X-100 单价6800元 数量1台'
    source = {'url': 'https://example.gov.cn/award-source-date', 'body_status': 'fetched',
              'body_text': body, 'source_type': 'tender_document',
              'publication_date': '2025-06-18'}
    raw = {'kind': 'award', 'brand': '厂商', 'model': 'X-100', 'amount': 6800,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
           'excerpt': body}
    result = validate_price(raw, source, {'brand': '厂商', 'model': 'X-100'})
    assert result['level'] == 'historical_configuration'
    assert result['date'] == '2025-06-18'


def test_parameter_units_direction_conditions_and_missing():
    required = {'value': 1, 'unit': 'L', 'direction': 'min'}
    observed = {'value': 1200, 'unit': 'ml', 'evidence': {'url': 'source'}}
    assert evaluate_parameter(required, observed) == 'satisfied'
    assert evaluate_parameter({**required, 'direction': 'max'}, observed) == 'violated'
    assert evaluate_parameter(required, {**observed, 'conditions': '特殊模式'}) == 'unknown'
    assert evaluate_parameter(required, {**observed, 'evidence': None}) == 'unknown'
    assert evaluate_parameter({'value': [10, 20], 'unit': '℃', 'direction': 'covers'},
                              {'value': [0, 30], 'unit': '℃', 'evidence': True}) == 'satisfied'


def test_confirm_idempotent_partial_results_report_and_restore(tmp_path):
    search = OfficialSearchService(providers=[FakeSearchProvider()])
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files', search_service=search)
    class Client:
        def analyze(self, **kwargs):
            return {'candidates': []}
    app.state.research_service.client_factory = Client
    with TestClient(app) as client:
        task_id = client.post('/api/v1/tasks', json={'scenario': '常规样品容量检测'}).json()['task']['id']
        prefix = f'/api/v1/tasks/{task_id}'
        payload = {'scenario': '常规样品容量检测', 'brand': '实验品牌', 'model': 'X-100', 'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}]}
        first = client.post(prefix + '/baseline/confirm', json=payload)
        assert first.status_code == 200
        run_id = first.json()['id']
        app.state.research_service.futures[run_id].result(timeout=10)
        second = client.post(prefix + '/baseline/confirm', json=payload)
        assert second.json()['id'] == run_id
        result = client.get(prefix + f'/research-runs/{run_id}').json()
        assert result['status'] == 'partial'
        assert result['lanes'] == {'price': 'completed', 'alternatives': 'completed'}
        assert client.get(prefix + '/baseline').json()['run']['id'] == run_id
        exported = client.post(prefix + f'/research-runs/{run_id}/report')
        assert exported.status_code == 200 and exported.content[:2] == b'PK'
        assert client.get('/api/v1/reports').json()['items']


def test_cancellation_does_not_get_overwritten(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    provider = FakeSearchProvider()
    def slow(query):
        entered.set(); gate.wait(5); return []
    provider.search = slow
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files', search_service=OfficialSearchService(providers=[provider]))
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': '容量检测'}).json()['task']['id']
        prefix = f'/api/v1/tasks/{tid}'
        run = client.post(prefix + '/baseline/confirm', json={'scenario': '容量检测', 'model': 'X1', 'parameters': [{'name': '容量', 'value': '10'}]}).json()
        assert entered.wait(3)
        assert client.post(prefix + f"/research-runs/{run['id']}/cancel").json()['status'] == 'cancelled'
        gate.set(); app.state.research_service.futures[run['id']].result(timeout=10)
        assert client.get(prefix + f"/research-runs/{run['id']}").json()['status'] == 'cancelled'


def test_model_table_projection_excludes_foreign_columns_and_regions():
    from backend.services.evidence_ownership import scoped_documents
    table = {'rows': [['型号', 'X100', 'X200'], ['容量', '10', '20']], 'source_location': 'page:1'}
    doc = {'pages': [{'text': '', 'regions': [{'label': 'table', 'text': '|型号|X100|X200|\n|---|---|---|\n|容量|10|20|'}]}], 'tables': [table]}
    result = scoped_documents({'model': 'X100'}, [doc], [{'model': 'X100'}, {'model': 'X200'}])[0]
    assert result['tables'][0]['rows'] == [['型号', 'X100'], ['容量', '10']]
    assert 'X200' not in str(result) and '20' not in str(result)


def test_equal_parameter_respects_units():
    assert evaluate_parameter({'value': 1, 'unit': 'L'}, {'value': 1000, 'unit': 'ml', 'evidence': True}) == 'satisfied'
    assert evaluate_parameter({'value': 1, 'unit': 'L'}, {'value': 1, 'unit': 'ml', 'evidence': True}) == 'violated'
    assert evaluate_parameter({'value': 1, 'unit': 'L'}, {'value': 1, 'unit': 'rpm', 'evidence': True}) == 'unknown'


def test_actual_university_award_keeps_line_item_separate_from_project_total():
    import json
    from pathlib import Path
    source = json.loads((Path(__file__).parent / 'fixtures/hbut_2019_award_snapshot.json').read_text(encoding='utf-8'))
    excerpt = '3 大容量冷冻离心机 湘仪DL-6M 湖南湘仪实验室仪器开发有限公司 1台 ￥ 74800.00'
    row = {'model': 'DL-6M', 'kind': 'award', 'amount_scope': 'single_item_total', 'amount': 74800,
           'amount_unit': '元', 'quantity': 1, 'currency': 'CNY', 'date': '2019-12-20', 'excerpt': excerpt}
    result = validate_price(row, source, {'model': 'DL-6M'})
    assert result['unit_price'] == 74800
    assert result['level'] == 'model_reference' and result['recent'] is False
    assert validate_price({**row, 'amount': 307330}, source, {'model': 'DL-6M'}) is None
    assert validate_price(row, source, {'model': 'VORTEX22K'}) is None


def test_restart_requires_explicit_continue_and_preserves_baseline(tmp_path):
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.procurement_research import ProcurementResearch
    repo = SQLiteRepository(tmp_path / 'restart.db')
    repo.put('tasks', 't1', {'id': 't1', 'scenario': '检测'})
    repo.put('procurement_records', 'r1', {'id': 'r1', 'kind': 'research', 'task_id': 't1', 'created_at': '2026-01-01', 'status': 'running', 'baseline': {'version': 1}, 'prices': [{'unit_price': 10}]}, task_id='t1')
    service = ProcurementResearch(repo, OfficialSearchService(providers=[FakeSearchProvider()]), lambda: None)
    assert service.get('t1', 'r1')['status'] == 'interrupted'
    assert service.get('t1', 'r1')['prices'] == [{'unit_price': 10}]
    assert not service.futures
    service.close(); repo.close()


def test_bad_parameter_range_rejected_before_research(tmp_path):
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files', search_service=OfficialSearchService(providers=[FakeSearchProvider()]))
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': '温度检测'}).json()['task']['id']
        response = client.post(f'/api/v1/tasks/{tid}/baseline/confirm', json={'model':'X1','scenario':'温度检测','parameters':[{'name':'温度','value':[50,0],'direction':'covers'}]})
        assert response.status_code == 422
        assert not app.state.research_service.futures


def test_hourly_cap_persists_across_ledger_connections(monkeypatch, tmp_path):
    from backend.infrastructure.usage_ledger import reserve_call
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'usage.db'))
    assert reserve_call('provider', 2)
    assert reserve_call('provider', 2)
    assert not reserve_call('provider', 2)


def test_document_cap_shared_and_nested_context_can_be_restored():
    from backend.infrastructure.call_budget import active_budget, reserve
    token = active_budget.set({'used': 0, 'limit': 2})
    try:
        assert reserve() and reserve()
        assert not reserve()
    finally:
        active_budget.reset(token)
    assert reserve()


def test_model_spacing_preserves_suffix_identity():
    assert contains_model('VORTEX 22K 技术参数', 'VORTEX22K')
    assert contains_model('X - 100', 'X100')
    assert not contains_model('X100S', 'X100')
    assert not contains_model('AX100', 'X100')


def test_official_domain_link_survives_html_and_rejects_lookalikes():
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    from backend.services.source_identity import verify_publisher
    html = '<p>示例仪器官方网站 <a href="https://vendor.example/products">访问官网</a></p>'
    body = PublicWebSearchProvider._clean_html(html)
    source = {'url': 'https://lab.example.edu.cn/partners', 'body_status': 'fetched', 'body_text': body}
    candidate = {'url': 'https://vendor.example/catalog'}
    assert verify_publisher(candidate, [source], '示例仪器')
    for fake in ['https://vendor.example.evil.invalid', 'https://tracker.example/?next=vendor.example']:
        assert not verify_publisher(candidate, [{**source, 'body_text': '示例仪器官方网站 ' + fake}], '示例仪器')
    assert not verify_publisher(candidate, [{**source, 'url': 'https://vendor.example/about'}], '示例仪器')


def test_confirmation_deduplication_across_database_connections(tmp_path):
    from backend.repositories.sqlite import SQLiteRepository
    from concurrent.futures import ThreadPoolExecutor
    repos = [SQLiteRepository(tmp_path / 'dedup.db') for _ in range(2)]
    def create(index):
        b = {'id': f'b{index}', 'task_id': 't', 'kind': 'baseline', 'digest': 'same', 'version': 1}
        r = {'id': f'r{index}', 'task_id': 't', 'kind': 'research', 'baseline_id': b['id'], 'status': 'queued'}
        return repos[index].create_procurement_run(b, r)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        assert sum(created for _, created in results) == 1
        assert results[0][0]['id'] == results[1][0]['id']
        assert len(repos[0].list('procurement_records', 't')) == 2
    finally:
        for repo in repos:
            repo.close()


def test_nonfinite_observed_range_cannot_qualify():
    assert evaluate_parameter({'value': [0, 20], 'unit': '℃', 'direction': 'covers'},
                              {'value': [float('-inf'), float('inf')], 'unit': '℃', 'evidence': True}) == 'unknown'
