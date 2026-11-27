"""Offline development regression; never a formal business blind test."""
import copy
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.repositories.sqlite import SQLiteRepository
from backend.infrastructure.deepseek import DeepSeekError
from backend.services.analysis_checkpoint import AnalysisCheckpoint
from backend.services.deepseek_analysis import DeepSeekAnalysisService, CANDIDATE_INVENTORY_PROMPT, VISUAL_DETAIL_PROMPT
from backend.services.evidence_ownership import project_table, scoped_documents
from backend.services.official_search import OfficialSearchService
from backend.services.supplemental_research import eligibility, prompt_packet, import_leads, report_id


def test_merged_cells_require_geometric_span_and_vertical_sections_stop():
    rows = [['型号', 'Q10', 'Q20', 'Q30'], ['噪声', '50', '', '60'], ['尺寸', '10', '20', '30']]
    cells = [{'row_start': 1, 'row_end': 2, 'col_start': 1, 'col_end': 3, 'text': '50'}]
    assert project_table(rows, 'Q20', ['Q10', 'Q30'], cells)[1] == ['噪声', '50']
    assert project_table(rows, 'Q20', ['Q10', 'Q30'])[1] == ['噪声', '']
    vertical = [['Q10', ''], ['容量', '10'], ['Q20', ''], ['容量', '90']]
    assert project_table(vertical, 'Q10', ['Q20']) == vertical[:2]


def test_ragged_table_and_ambiguous_heading_never_shift_foreign_data():
    rows = [['型号', 'Q10', 'Q20'], ['包装', '100 x 200'], ['容量', '1', '2']]
    assert project_table(rows, 'Q20', ['Q10']) == [['型号', 'Q20'], ['容量', '2']]
    document = {'pages': [{'regions': [{'label': 'title', 'text': 'Q10'}, {'label': 'table', 'text': '|型号|Q20|\n|容量|90|'}, {'label': 'text', 'text': '温度80'}]}]}
    result = scoped_documents({'model': 'Q10'}, [document], [{'model': 'Q10'}, {'model': 'Q20'}])
    assert '温度80' not in str(result)


def test_checkpoint_resumes_failed_document_without_repaying_success(tmp_path):
    repo = SQLiteRepository(tmp_path/'checkpoint.db')
    cp = AnalysisCheckpoint(repo, 't')
    docs = [{'id': 'd1', 'pages': [{'number': 1, 'source_location': 'page:1', 'text': 'Model Q10'}]},
            {'id': 'd2', 'pages': [{'number': 1, 'source_location': 'page:1', 'text': 'Model Q20'}]}]
    class Model:
        calls = []
        fail = True
        def analyze(self, system_prompt, user_prompt):
            self.calls.append(user_prompt)
            if 'd2' in user_prompt and self.fail:
                raise DeepSeekError('provider_timeout', '超时')
            return {'candidates': []}
    model = Model()
    service = DeepSeekAnalysisService(model, cp)
    service.analyze(scenario='检测', documents=docs)
    assert len(model.calls) == 2
    model.fail = False
    service.analyze(scenario='检测', documents=docs)
    assert len(model.calls) == 3 and 'd2' in model.calls[-1]
    service.analyze(scenario='检测', documents=docs)
    assert len(model.calls) == 3
    # Edited source evidence invalidates only the corresponding document node.
    docs[0]['pages'][0]['text'] += ' revised'
    service.analyze(scenario='检测', documents=docs)
    assert len(model.calls) == 4
    repo.close()


def test_cancel_after_call_saves_result_before_stop(tmp_path):
    repo = SQLiteRepository(tmp_path/'cancel.db'); cp = AnalysisCheckpoint(repo, 't')
    class Model:
        calls = 0
        def analyze(self, **kwargs):
            self.calls += 1; cp.set_control(cancelled=True)
            return {'candidates': []}
    model = Model(); service = DeepSeekAnalysisService(model, cp)
    docs = [{'id': 'd', 'pages': [{'number': 1, 'text': 'Model Q10', 'source_location': 'page:1'}]}]
    with pytest.raises(DeepSeekError, match='分析已停止'):
        service.analyze(scenario='检测', documents=docs)
    cp.set_control(cancelled=False)
    service.analyze(scenario='检测', documents=docs)
    assert model.calls == 1
    repo.close()


def test_parameter_truncation_resumes_completed_region(tmp_path):
    repo = SQLiteRepository(tmp_path/'regions.db'); cp = AnalysisCheckpoint(repo, 't')
    docs = [{'id': 'd', 'pages': [{'number': 1, 'source_location': 'page:1', 'text': 'Model Q10\n' + 'technical information\n' * 20}]}]
    class Model:
        calls = 0
        def analyze(self, system_prompt, user_prompt):
            self.calls += 1
            if system_prompt == CANDIDATE_INVENTORY_PROMPT:
                return {'candidates': [{'model': 'Q10', 'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': 'Model Q10'}]}]}
            if system_prompt == VISUAL_DETAIL_PROMPT and self.calls == 2:
                raise DeepSeekError('model_output_truncated', '截断')
            if self.calls == 4:
                raise DeepSeekError('provider_timeout', '超时')
            return {'candidates': [{'model': 'Q10', 'fields': []}]}
    model = Model(); service = DeepSeekAnalysisService(model, cp)
    first = service.analyze(scenario='检测', documents=docs)
    assert first['status'] == 'partial'
    service.analyze(scenario='检测', documents=docs)
    assert model.calls == 6  # replayed region plus the specialized empty-parameter retry
    repo.close()


@pytest.fixture
def research(tmp_path):
    class Provider:
        name = 'offline'
        reads = []
        bodies = {}
        def search(self, query): return []
        def fetch(self, url, timeout):
            self.reads.append(url)
            return self.bodies[url]
    provider = Provider()
    app = create_app(database_path=tmp_path/'app.db', storage_dir=tmp_path/'files', search_service=OfficialSearchService(providers=[provider]))
    class Model:
        def analyze(self, **kwargs): return {'candidates': []}
    service = app.state.research_service; service.client_factory = Model
    with TestClient(app) as client:
        tid = client.post('/api/v1/tasks', json={'scenario': '容量检测'}).json()['task']['id']
        run = client.post(f'/api/v1/tasks/{tid}/baseline/confirm', json={'brand': '甲厂', 'model': 'Q10', 'scenario': '容量检测', 'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}]}).json()
        service.futures[run['id']].result(10)
        yield service, service.get(tid, run['id']), provider, client


def test_supplement_gated_by_saved_specific_gaps_not_status_or_count(research):
    service, run, _, _ = research
    assert eligibility(service.repo, run)['allowed']
    assert not eligibility(service.repo, {**run, 'status': 'failed'})['allowed']
    saved = service.repo.get('reports', report_id(run))
    saved['snapshot']['lanes']['price'] = 'failed'
    service.repo.put('reports', report_id(run), saved, task_id=run['task_id'])
    assert not eligibility(service.repo, run)['allowed']


def test_supplement_keeps_seventy_percent_candidate_available_for_gap_research(research):
    service, run, _, _ = research
    saved = service.repo.get('reports', report_id(run))
    saved['snapshot']['prices'] = [{'level': 'historical_configuration'}]
    saved['snapshot']['alternatives'] = [{
        'model': 'Q20',
        'status': 'ineligible',
        'comparison_disposition': 'comparable_candidate',
        'matrix': [{
            'required': {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'},
            'status': 'violated',
        }],
        'prices': [],
    }]
    service.repo.put('reports', report_id(run), saved, task_id=run['task_id'])
    gate = eligibility(service.repo, run)
    assert gate['allowed']
    assert not any(g['kind'] == 'discovery' for g in gate['gaps'])
    assert any(g['kind'] == 'price' and g['model'] == 'Q20' for g in gate['gaps'])
    saved['snapshot']['lanes']['price'] = 'completed'
    saved['snapshot']['prices'] = [{'level': 'historical_configuration'}]
    saved['snapshot']['alternatives'] = [{'model': 'Q20', 'status': 'eligible', 'matrix': [], 'prices': [{'level': 'historical_configuration'}]}]
    service.repo.put('reports', report_id(run), saved, task_id=run['task_id'])
    assert not eligibility(service.repo, run)['allowed']


def test_prompt_does_not_export_documents_contact_or_secrets(research):
    service, run, _, _ = research
    saved = service.repo.get('reports', report_id(run))
    saved['snapshot']['baseline']['scenario'] = '容量检测 abc@example.com 13812345678 sk-exampletestvalue'
    saved['snapshot']['sources'] = [{'url': 'https://example.edu.cn/page?token=secret', 'body_text': '销售全文不要导出'}]
    service.repo.put('reports', report_id(run), saved, task_id=run['task_id'])
    packet = prompt_packet(service.repo, run)
    assert all(s not in packet['prompt'] for s in ('销售全文不要导出', 'token=secret', 'abc@example.com', '13812345678', 'sk-exampletestvalue'))
    assert packet['report_id'] == report_id(run)


def test_import_rechecks_source_ignores_asserted_authority_and_keeps_report(research):
    service, run, provider, client = research
    original = copy.deepcopy(service.repo.get('reports', report_id(run)))
    official = 'https://example.edu.cn/award'; fake = 'https://dealer.example/price'
    text = f'{date.today().isoformat()} 甲厂 Q10 数量1台 中标单价12000元'
    provider.bodies = {official: text, fake: text}
    row = {'model': 'Q10', 'kind': 'award', 'amount': 12000, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'date': date.today().isoformat(), 'excerpt': text}
    payload = {'report_id': report_id(run), 'sources': [{'url': official}, {'url': fake, 'source_type': 'manufacturer_website', 'body_text': text}],
               'prices': [{**row, 'url': official}, {**row, 'url': fake}, {**row, 'url': official, 'amount': 999}], 'candidates': []}
    response = client.post(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}/supplement/import", json=payload)
    assert response.status_code == 200
    child = response.json()
    assert len(child['prices']) == 1 and child['prices'][0]['unit_price'] == 12000
    assert child['parent_report_id'] == report_id(run)
    assert service.repo.get('reports', report_id(run)) == original
    assert service.repo.get('reports', report_id(child))
    assert len(provider.reads) == 2
    repeat = import_leads(service, run['task_id'], run['id'], payload)
    assert repeat['id'] == child['id'] and len(provider.reads) == 2
    assert service.get(run['task_id'], run['id'])['usage']['fetch'] == 2


def test_import_does_not_promote_unverified_candidate_parameters(research):
    service, run, provider, _ = research
    authority, product = 'https://example.edu.cn/partners', 'https://vendor.example/product'
    provider.bodies = {authority: '乙厂官方网站 https://vendor.example/product', product: '乙厂 Q20 容量120 ml'}
    lead = {'brand': '乙厂', 'model': 'Q20', 'url': product, 'excerpt': '乙厂 Q20 容量120 ml', 'parameters': [
        {'name': '容量', 'value': 999, 'unit': 'ml', 'url': product, 'excerpt': '乙厂 Q20 容量120 ml'}]}
    payload = {'report_id': report_id(run), 'sources': [{'url': authority}, {'url': product}], 'candidates': [lead]}
    child = import_leads(service, run['task_id'], run['id'], payload)
    assert child['alternatives'][0]['status'] == 'needs_review'
    lead['parameters'][0]['value'] = 120
    verified = import_leads(service, run['task_id'], run['id'], payload)
    assert verified['alternatives'][0]['status'] == 'eligible'
    assert len(provider.reads) == 2


@pytest.mark.parametrize('url', ['file:///C:/secrets', 'http://127.0.0.1/', 'http://[::1]/', 'https://user:pass@example.com'])
def test_import_rejects_nonpublic_or_credential_urls(research, url):
    service, run, provider, _ = research
    with pytest.raises(ValueError):
        import_leads(service, run['task_id'], run['id'], {'report_id': report_id(run), 'sources': [{'url': url}]})
    assert not provider.reads


def test_html_relative_attachments_rows_and_late_model_window():
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    from backend.services.research_sources import price_regions, attachments
    body = PublicWebSearchProvider._clean_html('<p>成交公告</p><a href="../file/items.pdf">明细</a><table><tr><td>项目总价100000元</td></tr><tr><td>Q10</td><td>单价1000元</td></tr></table>', 'https://example.gov.cn/notices/1')
    assert attachments({'url': 'https://example.gov.cn/notices/1', 'body_text': body}) == ['https://example.gov.cn/file/items.pdf']
    assert '项目总价100000元\nQ10' in body
    regions = price_regions('不相关内容\n'*10000 + body, 'Q10')
    assert regions and regions[0]['start'] > 24000 and '单价1000元' in regions[0]['text']
    with pytest.raises(ValueError):
        PublicWebSearchProvider.decode_body(b'%PDF broken', 'application/pdf')
    nested = PublicWebSearchProvider._clean_html('<table><tr><td><p>Q10</p></td><td><p>数量1台</p></td><td><p>单价1000元</p></td></tr></table>')
    assert '单价1000元' in price_regions(nested, 'Q10')[0]['text']


def test_analysis_claim_is_atomic_across_connections(tmp_path):
    a, b = SQLiteRepository(tmp_path/'lease.db'), SQLiteRepository(tmp_path/'lease.db')
    try:
        owner = a.claim_analysis('t')
        assert owner and not b.claim_analysis('t')
        b.release_analysis('t', 'wrong')
        assert not b.claim_analysis('t')
        a.release_analysis('t', owner)
        assert b.claim_analysis('t')
    finally:
        a.close(); b.close()


def test_continuation_requires_explicit_adjacent_matching_table():
    tables = [{'rows': [['型号', 'A10', 'A20'], ['尺寸', '10', '20']], 'source_location': 'page:1/table:1'},
              {'rows': [['容量', '1', '2']], 'source_location': 'page:2/table:1'}]
    doc = {'pages': [{'page': 2, 'text': '技术参数 续表'}], 'tables': tables}
    result = scoped_documents({'model': 'A20'}, [doc], [{'model': 'A10'}, {'model': 'A20'}])[0]
    assert result['tables'][1]['rows'][-1] == ['容量', '2']
    doc['pages'][0]['text'] = '其他产品'
    assert len(scoped_documents({'model': 'A20'}, [doc], [{'model': 'A10'}, {'model': 'A20'}])[0]['tables']) == 1


def test_nested_imports_share_root_budget_and_no_results_is_preserved(research):
    service, run, provider, _ = research
    urls = [f'https://example.edu.cn/award{i}' for i in range(3)]
    provider.bodies = {u: '采购公告 没有相关型号' for u in urls}
    run['limits']['fetch'] = 2; service.save(run)
    first = import_leads(service, run['task_id'], run['id'], {'report_id': report_id(run), 'sources': [{'url': urls[0]}]})
    second = import_leads(service, run['task_id'], first['id'], {'report_id': report_id(first), 'sources': [{'url': urls[1]}]})
    third = import_leads(service, run['task_id'], run['id'], {'report_id': report_id(run), 'sources': [{'url': urls[2]}]})
    assert len(provider.reads) == 2 and third['supplement_retryable']
    assert service.get(run['task_id'], run['id'])['usage']['fetch'] == 2
    empty = import_leads(service, run['task_id'], second['id'], {'report_id': report_id(second), 'not_found': ['同配置价格'], 'sources': []})
    assert any('外部补查未找到' in gap for gap in empty['gaps'])


def test_parameter_value_is_not_substring_or_wrong_unit():
    from backend.services.procurement_rules import grounded_parameter
    text = 'Q20 容量120 ml，温度20 ℃'
    source = {'body_status': 'fetched', 'source_type': 'manufacturer_website', 'body_text': text}
    obs = {'value': 120, 'unit': 'ml', 'excerpt': 'Q20 容量120 ml'}
    assert grounded_parameter(obs, source, 'Q20', '容量')
    assert not grounded_parameter({**obs, 'value': 20}, source, 'Q20', '容量')
    assert not grounded_parameter({**obs, 'unit': 'L'}, source, 'Q20', '容量')
    assert not grounded_parameter({**obs, 'value': 20, 'excerpt': text}, source, 'Q20', '容量')


def test_publisher_cross_paragraph_brand_and_cdn_scope():
    from backend.services.source_identity import verify_publisher
    source = {'url': 'https://example.edu.cn/links', 'body_status': 'fetched', 'body_text': '甲厂官网\n乙厂官网 https://vendor.example/'}
    assert not verify_publisher({'url': 'https://vendor.example/'}, [source], '甲厂')
    verified = {'url': 'https://vendor.example/', 'body_status': 'fetched', 'body_text': '技术文件 https://cdn.example/manual.pdf', 'source_type': 'manufacturer_website', 'publisher_evidence': {'brand': '乙厂'}}
    relation = verify_publisher({'url': 'https://cdn.example/manual.pdf'}, [verified], '乙厂')
    assert relation['scope'] == 'exact_url'
    assert not verify_publisher({'url': 'https://cdn.example/unrelated.pdf'}, [verified], '乙厂')


def test_visual_crop_checkpoint_retries_only_failed_region(tmp_path):
    from io import BytesIO
    from PIL import Image
    from backend.services.document_ingestion import DocumentIngestionService
    from backend.services.analysis_checkpoint import current_checkpoint
    from backend.services.vision import VisionAdapterError, VisionResult
    from backend.schemas.documents import DocumentRecord, DocumentImage
    repo = SQLiteRepository(tmp_path/'vision.db'); cp = AnalysisCheckpoint(repo, 't')
    stream = BytesIO(); Image.new('RGB', (200, 400), 'white').save(stream, format='PNG')
    class Vision:
        provider = 'fake'; calls = []; fail = True
        def analyze(self, image_bytes, *, media_type, source_location):
            self.calls.append(source_location)
            if source_location == 'page:1/rendered':
                raise VisionAdapterError('vision_output_truncated', '截断')
            if source_location.endswith('crop:1') and self.fail:
                raise VisionAdapterError('provider_timeout', '超时')
            return VisionResult(text='Q10 容量100 ml')
    adapter = Vision(); ingestion = DocumentIngestionService(tmp_path/'files')
    doc = DocumentRecord('d', 'd.pdf', '', 'pdf', 'application/pdf', None, None, None)
    image = DocumentImage(1, 'page:1/rendered', 'image/png'); doc.images.append(image)
    token = current_checkpoint.set(cp)
    try:
        ingestion._vision_image(doc, image, stream.getvalue(), adapter)
        assert image.ocr_status == 'failed' and len(adapter.calls) == 3
        adapter.fail = False
        ingestion._vision_image(doc, image, stream.getvalue(), adapter)
        assert image.ocr_status == 'completed' and len(adapter.calls) == 4
        assert adapter.calls[-1].endswith('crop:1')
    finally:
        current_checkpoint.reset(token); repo.close()


def test_importing_app_factory_does_not_open_default_database(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, '-c', 'from backend.main import create_app; assert callable(create_app)'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path/'data/app.sqlite3').exists()
