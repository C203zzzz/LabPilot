from fastapi.testclient import TestClient
from backend.main import create_app


def test_unknown_analysis_fields_remain_unknown_after_product_confirmation(tmp_path):
    with TestClient(create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')) as client:
        created = client.post('/api/v1/tasks', json={'scenario': '采购离心机'}).json()
        tid = created['task']['id']
        candidate = {'candidate_id': 'candidate-qa', 'brand': 'Eppendorf', 'model': '5420', 'sources': [], 'fields': [
            {'field_name': 'price', 'status': 'unknown', 'value': 'unknown', 'sources': []},
        ]}
        client.app.state.repository.put('reports', 'analysis-qa', {'kind': 'deepseek_analysis', 'analysis_id': 'analysis-qa', 'candidates': [candidate]}, task_id=tid)
        body = [{'analysis_id': 'analysis-qa', 'candidate_id': 'candidate-qa', 'status': 'confirmed', 'field_statuses': {}}]
        result = client.post(f'/api/v1/tasks/{tid}/analysis/deepseek/decisions', json=body)
        assert result.status_code == 200
        assert result.json()['items'][0]['fields'][0]['status'] == 'unknown'
        assert client.app.state.repository.list('facts', tid) == []
        candidate['fields'][0]['status'] = 'conflicted'
        client.app.state.repository.put('reports', 'analysis-qa', {'kind': 'deepseek_analysis', 'analysis_id': 'analysis-qa', 'candidates': [candidate]}, task_id=tid)
        assert client.post(f'/api/v1/tasks/{tid}/analysis/deepseek/decisions', json=body).status_code == 409


def test_search_plan_uses_instrument_subject_without_long_instructions(tmp_path):
    with TestClient(create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files')) as client:
        created = client.post('/api/v1/tasks', json={'scenario': '采购多功能酶标仪，预算上限10万元；价格未知不要猜测'}).json()
        tid = created['task']['id']
        client.post(f'/api/v1/tasks/{tid}/requirements/confirm', json={'requirement': created['requirement']})
        plan = client.post(f'/api/v1/tasks/{tid}/search/plan').json()
        assert len(plan['queries']) == 4
        assert all(q['text'].startswith('多功能酶标仪 ') and '不要猜测' not in q['text'] for q in plan['queries'])
