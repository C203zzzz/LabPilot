"""Focused defect regressions; these are not the user's v1 business rounds."""
import copy
import hashlib
import io
import json
from zipfile import ZipFile

from backend.services.procurement_research import ON_DEMAND_ENV
from backend.services.procurement_rules import parameter_row, grounded_parameter
from backend.services.report_library import export_reports
from backend.services.research_sources import snapshot_path
from backend.repositories.sqlite import SQLiteRepository
from backend.infrastructure.search_providers import PublicWebSearchProvider
from tests.test_reliability_development import research


def test_portable_export_checks_original_hash_and_preserves_record(tmp_path):
    repo = SQLiteRepository(tmp_path / 'app.db')
    original = b'<html>original evidence</html>'
    snap = snapshot_path(repo, original)
    record = {'report': {'sections': []}, 'snapshot': {'sources': [
        {'url': 'https://example.edu.cn/a', 'body_text': '正文', 'snapshot': snap},
        {'url': 'https://example.edu.cn/b', 'body_text': '旧正文'},
        {'url': 'https://example.edu.cn/c', 'snapshot': {**snap, 'sha256': 'a'*64}},
    ]}}
    before = copy.deepcopy(record)
    items = [{'id': 'r', 'title': '测试', 'version': 1, 'created_at': None, 'scenario': '用途'}]
    exported = export_reports(items, {'r': record}, tmp_path / 'research-snapshots')
    with ZipFile(io.BytesIO(exported)) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read(next(n for n in z.namelist() if n.endswith('-来源清单.json'))))
        assert [e['original_status'] for e in manifest] == ['included', 'not_captured', 'missing_or_invalid']
        assert z.read(manifest[0]['original_file']) == original
        assert z.read(manifest[1]['text_file']).decode() == '旧正文'
    assert record == before
    repo.close()


def test_conflicting_official_parameters_cannot_be_silently_replaced():
    req = {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}
    sources = [{'url': f'https://vendor.example/{v}', 'source_type': 'manufacturer_website',
                'body_status': 'fetched', 'body_text': f'Q20 容量{v} ml'} for v in (120, 80)]
    observations = [{'name': '容量', 'value': v, 'unit': 'ml', 'url': s['url'], 'excerpt': s['body_text']} for v, s in zip((120, 80), sources)]
    first = parameter_row(req, observations[:1], sources, 'Q20')
    assert first['status'] == 'satisfied'
    combined = parameter_row(req, observations[1:], sources, 'Q20', first)
    assert combined['conflict'] and combined['status'] == 'unknown' and combined['observed'] is None
    assert len(combined['observations']) == 2
    assert parameter_row(req, [], sources, 'Q20', combined)['conflict']
    assert parameter_row(req, observations, sources, 'Q20')['conflict']


def test_thousands_separator_and_negative_temperature_are_grounded():
    text = 'Q20 转速22,000 r/min；温度-20～40 ℃'
    source = {'body_status': 'fetched', 'source_type': 'manufacturer_website', 'body_text': text}
    assert grounded_parameter({'value': 22000, 'unit': 'r/min', 'excerpt': text}, source, 'Q20', '转速')
    assert grounded_parameter({'value': [-20, 40], 'unit': '℃', 'excerpt': text}, source, 'Q20', '温度')
    body = 'Q20 最高转速 角转子22000 r/min 水平转子4000 r/min'
    source['body_text'] = body
    obs = {'value': 22000, 'unit': 'r/min', 'conditions': '角转子', 'excerpt': body}
    assert grounded_parameter(obs, source, 'Q20', '角转子最高转速')
    assert not grounded_parameter({**obs, 'value': 4000}, source, 'Q20', '角转子最高转速')
    assert grounded_parameter({**obs, 'value': 4000, 'conditions': '水平转子'}, source, 'Q20', '水平转子最高转速')


def test_unverified_fetched_source_can_supply_provisional_parameter_evidence():
    req = {'name': '温度设置范围', 'value': [-20, 40], 'unit': '℃', 'direction': 'covers'}
    url = 'https://dealer.example/ultra'
    source = {'url': url, 'body_status': 'fetched', 'source_type': 'unknown',
              'purpose': 'discovery', 'body_text': '[表格按列展开] Ultra 5.0 温度范围 -10℃ to 40℃'}
    observation = {'name': req['name'], 'value': [-10, 40], 'unit': '℃', 'conditions': '',
                   'url': url, 'excerpt': source['body_text']}

    assert parameter_row(req, [observation], [source], 'Ultra 5.0')['observed'] is None
    row = parameter_row(req, [observation], [source], 'Ultra 5.0', allow_unverified=True)
    assert row['status'] == 'violated'
    assert row['evidence_status'] == 'provisional'
    assert row['observed']['evidence_status'] == 'provisional'


def test_provisional_parameter_source_cannot_use_tender_body():
    req = {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}
    source = {'url': 'https://example.gov.cn/award', 'body_status': 'fetched',
              'source_type': 'unknown', 'purpose': 'tender', 'body_text': 'Q20 容量120 ml'}
    observation = {'name': '容量', 'value': 120, 'unit': 'ml', 'url': source['url'],
                   'excerpt': source['body_text']}
    assert parameter_row(req, [observation], [source], 'Q20', allow_unverified=True)['observed'] is None


def test_expired_worker_cannot_revive_or_release_new_lease(tmp_path):
    repo = SQLiteRepository(tmp_path / 'app.db')
    first = repo.claim_analysis('t')
    repo.db.execute('UPDATE analysis_leases SET expires=0'); repo.db.commit()
    assert not repo.renew_analysis('t', first)
    second = repo.claim_analysis('t')
    assert second != first
    repo.release_analysis('t', first)
    assert repo.active_analysis('t') == second
    repo.close()


def test_cached_search_restores_attachment_and_publication_without_search(research):
    from backend.services.procurement_research import now
    service, run, provider, _ = research
    parent, attachment = 'https://example.edu.cn/award', 'https://files.example/item.pdf'
    body = '2026-09-19 成交公告 明细 ' + attachment
    provider.bodies = {attachment: 'Q10 单价100元'}
    cache_id = 'search-cache-' + hashlib.sha256(b'cached|tender').hexdigest()
    service.save({'id': cache_id, 'task_id': run['task_id'], 'created_at': now(), 'cache_version': 4, 'sources': [
        {'url': parent, 'body_status': 'fetched', 'body_text': body, 'source_type': 'tender_document'}]})
    before = run['usage']['search']
    service.query(run, 'cached', 'tender')
    result = next(s for s in run['sources'] if s['url'] == attachment)
    assert result['source_type'] == 'tender_document'
    assert result['publication_context']['url'] == parent
    assert run['usage']['search'] == before and provider.reads == [attachment]
    service.query(run, 'cached', 'tender')
    assert provider.reads == [attachment]


def test_failed_attachment_remains_recoverable(research):
    service, run, provider, _ = research
    attachment = 'https://files.example/item.pdf'
    parent = {'url': 'https://example.edu.cn/award', 'body_status': 'fetched', 'body_text': '成交公告 ' + attachment, 'source_type': 'tender_document'}
    run['sources'].append(parent)
    service.fetch_attachments(run, parent)
    assert run['unresolved_source_errors']
    provider.bodies = {attachment: 'Q10 单价100元'}
    run['unresolved_source_errors'] = False
    run['completed_queries'].append('cached')
    service.query(run, 'cached', 'tender')
    assert next(s for s in run['sources'] if s['url'] == attachment)['body_status'] == 'fetched'
    assert not run['unresolved_source_errors']


def test_office_rows_and_legacy_html_charset():
    buf = io.BytesIO()
    with ZipFile(buf, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>成交明细</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Q10</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>单价100元</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    assert PublicWebSearchProvider.decode_body(buf.getvalue(), 'application/octet-stream') == '成交明细\nQ10 | 单价100元'
    import openpyxl
    wb = openpyxl.Workbook(); wb.active.append(['Q10', '单价100元']); stream = io.BytesIO(); wb.save(stream); wb.close()
    assert 'Q10 | 单价100元' in PublicWebSearchProvider.decode_body(stream.getvalue(), 'application/octet-stream')
    assert PublicWebSearchProvider.decode_body('<p>采购明细</p>'.encode('gbk'), 'text/html; charset=gb2312') == '采购明细'


def test_measured_margin_does_not_promote_negated_optional_preference():
    from backend.services.procurement_rules import comparison_details
    source = {'url': 'https://vendor.example/q', 'source_type': 'manufacturer_website', 'body_status': 'fetched', 'body_text': 'Q20 容量120 ml，不支持自动清洗'}
    req = {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}
    row = parameter_row(req, [{'name': '容量', 'value': 120, 'unit': 'ml', 'url': source['url'], 'excerpt': source['body_text']}], [source], 'Q20')
    details = comparison_details({'preferences': '自动清洗、远程控制'}, {'model': 'Q20', 'matrix': [row]}, [source])
    assert details['stronger_parameters'][0]['name'] == '容量'
    assert [p['status'] for p in details['preference_checks']] == ['needs_review', 'unknown']


def test_raster_closed_borders_keep_distinct_and_shared_cells():
    import cv2
    import numpy as np
    from backend.services.raster_tables import ruled_cells
    image = np.full((400, 600, 3), 255, np.uint8)
    for y in range(5, 396, 65):
        cv2.line(image, (5,y), (595,y), (0,0,0), 1)
    for x in (5,155,305,455,595):
        cv2.line(image, (x,5), (x,395), (0,0,0), 1)
    # Erase the internal border of one genuinely merged pair only.
    cv2.line(image,(305,136),(305,199),(255,255,255),3)
    cells = ruled_cells(image)
    assert cells
    assert any(c['row_start']==2 and c['col_start']==1 and c['col_end']==3 for c in cells)
    assert any(c['row_start']==1 and c['col_start']==1 and c['col_end']==2 for c in cells)


def test_failed_decode_retains_original_and_retry_uses_local_bytes(research):
    service, run, _, _ = research
    class Reader(PublicWebSearchProvider):
        reads = 0
        fail = True
        def fetch_raw(self, url, timeout):
            self.reads += 1
            return b'original', 'application/pdf'
        def decode_body(self, payload, content_type, url=''):
            if self.fail:
                raise ValueError('parser unavailable')
            return 'Q10 单价100元'
    reader = Reader(); service.search.providers = [reader]
    url = 'https://example.edu.cn/original.pdf'
    first = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})
    assert first['body_status'] == 'failed' and first['snapshot']['size'] == 8
    reader.fail = False
    run['limits']['fetch'] = run['usage']['fetch']
    second = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})
    assert second['body_status'] == 'fetched' and second['cache_hit'] and reader.reads == 1


def test_empty_fetched_body_cache_is_revalidated_instead_of_promoted(research):
    from backend.services.procurement_research import now
    service, run, provider, _ = research
    url = 'https://example.edu.cn/revalidate'
    key = 'body-cache-' + hashlib.sha256(url.encode()).hexdigest()
    service.save({
        'id': key, 'task_id': run['task_id'], 'kind': 'body_cache',
        'created_at': now(), 'cache_version': 4,
        'body': {'body_status': 'fetched', 'body_text': '   '},
    })
    provider.bodies[url] = 'Q10 成交公告，预算 12000 元'
    before = run['usage']['fetch']
    source = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})
    assert source['body_status'] == 'fetched'
    assert source['body_text'] == provider.bodies[url]
    assert not source.get('cache_hit')
    assert provider.reads == [url]
    assert run['usage']['fetch'] == before + 1


def test_empty_fetched_search_cache_is_refetched_before_query_completion(research):
    from backend.services.procurement_research import SEARCH_STRATEGY_VERSION, now
    service, run, provider, _ = research
    text, purpose = 'cache body consistency', 'tender'
    url = 'https://example.edu.cn/award'
    cache_scope = f'|procurement-search-v{SEARCH_STRATEGY_VERSION}'
    cache_id = 'search-cache-' + hashlib.sha256((text + '|' + purpose + cache_scope).encode()).hexdigest()
    service.save({
        'id': cache_id, 'task_id': run['task_id'], 'kind': 'search_cache',
        'created_at': now(), 'cache_version': SEARCH_STRATEGY_VERSION,
        'sources': [{'url': url, 'source_type': 'tender_document', 'body_status': 'fetched', 'body_text': ''}],
    })
    provider.bodies[url] = 'Q10 成交公告，预算 12000 元'
    service.fetch_attachments = lambda *args: None
    service.query(run, text, purpose)
    saved = next(source for source in run['sources'] if source['url'] == url)
    assert saved['body_status'] == 'fetched' and saved['body_text'] == provider.bodies[url]
    assert provider.reads == [url]
    assert text in run['completed_queries']


def test_alternative_raw_evidence_is_viewable_without_becoming_price(research):
    service, run, _, _ = research
    run['status'] = 'partial'
    run['round'] = 99
    run['lanes'] = {'price': 'completed', 'alternatives': 'completed'}
    run['prices'] = []
    run['alternatives'] = [{
        'brand': '乙厂', 'model': 'Q20', 'url': 'https://vendor.example/q20',
        'status': 'needs_review', 'matrix': [{
            'required': {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'},
            'observed': None, 'status': 'unknown',
            'raw_evidence': [{'url': 'https://vendor.example/q20', 'excerpt': 'Q20 容量120 ml', 'evidence_status': 'unparsed'}],
        }], 'prices': [],
    }]
    service.save(run)
    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s['key'] == 'alternatives')
    item = section['items'][0]
    assert any(e.get('excerpt') == 'Q20 容量120 ml' for e in item['evidence'])
    alternative_prices = next(s for s in report['sections'] if s['key'] == 'alternative_prices')
    assert alternative_prices['items'] == []


def test_republished_award_records_reach_the_report_as_second_hand_evidence(research):
    """A republished record must be shown with its link and its grade.

    The live run reported "no same-model transaction price" while 120shop.cn held
    nine dated XKA-2200 award rows. They now reach the report; this pins that the
    report actually renders them, keeps the clickable link, and never lets them
    claim the first-hand evidence slot.
    """
    service, run, _, _ = research
    run['status'] = 'partial'
    run['round'] = 98
    run['lanes'] = {'price': 'completed', 'alternatives': 'completed'}
    run['prices'] = []
    run['republished_prices'] = [{
        'model': 'XKA-2200', 'brand': '湘仪', 'amount': 4.70, 'amount_unit': '万元',
        'quantity': None, 'amount_scope': 'unit', 'currency': 'CNY', 'date': '2024-03-28',
        'configuration': 'unknown', 'purchaser': '北京天坛医院', 'supplier': '上药康德乐（北京）医药有限公司',
        'evidence_grade': 'secondary', 'unit_price': 47000.0,
        'url': 'https://www.120shop.cn/i890850380', 'source_type': 'unknown',
        'excerpt': '免疫血液学用离心机 | 湘仪 | XKA-2200 | 北京天坛医院 | 上药康德乐（北京）医药有限公司 | 2024-03-28 | 4.70万元 | 查看',
    }]
    service.save(run)
    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s['key'] == 'republished')
    item = section['items'][0]
    assert item['label'] == '北京天坛医院'
    assert '47,000' in item['text'] and '2024-03-28' in item['text']
    assert '二手' in item['text']
    # The configuration column does not exist on the aggregator page, so the
    # report must not claim the *announcement* withheld it.
    assert '来源页未提供配置' in item['text']
    assert '公告未提供' not in item['text']
    assert item['evidence'][0]['url'] == 'https://www.120shop.cn/i890850380'
    assert item['evidence'][0]['evidence_status'] == 'secondary'
    # The first-hand slot stays empty: a republished row is not an award price.
    assert next(s for s in report['sections'] if s['key'] == 'prices')['items'] == []


def test_unparsed_parameter_value_is_retained_with_original_citation():
    source = {
        'url': 'https://vendor.example/q20',
        'body_status': 'fetched',
        'source_type': 'unknown',
        'body_text': 'Q20 技术说明\n容量：约120 ml（以实物为准）',
    }
    row = parameter_row(
        {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'},
        [{'name': '容量', 'value': '约120 ml（未标定）', 'unit': 'ml', 'url': source['url'],
          'excerpt': 'Q20 技术说明\n容量：约120 ml（以实物为准）'}],
        [source], 'Q20', allow_unverified=True,
    )
    assert row['status'] == 'unknown'
    assert row['evidence_status'] == 'unparsed'
    evidence = row['raw_evidence'][0]
    assert evidence['raw_value'] == '约120 ml（未标定）'
    assert evidence['url'] == source['url']
    assert '容量：约120 ml' in evidence['excerpt']


def test_killed_process_lease_blocks_until_expiry_without_losing_nodes(tmp_path):
    import subprocess
    import sys
    import os
    from pathlib import Path
    database = tmp_path/'shared.db'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    code = '''import sys
from backend.repositories.sqlite import SQLiteRepository
r=SQLiteRepository(sys.argv[1])
owner=r.claim_analysis('task')
r.put('procurement_records','saved-node',{'id':'saved-node','status':'completed','result':{'value':120}},task_id='task')
print(owner,flush=True)
sys.stdin.read()
'''
    worker = subprocess.Popen([sys.executable,'-c',code,str(database)],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        owner = worker.stdout.readline().strip()
        assert owner
        repo = SQLiteRepository(database)
        assert not repo.claim_analysis('task')
        worker.terminate(); worker.wait(timeout=10)
        assert repo.active_analysis('task') == owner
        assert repo.get('procurement_records','saved-node')['result']['value'] == 120
        # Advance the persisted expiry; no production duration is shortened.
        repo.db.execute('UPDATE analysis_leases SET expires=0');repo.db.commit()
        assert repo.claim_analysis('task')
        assert repo.get('procurement_records','saved-node')['status']=='completed'
        repo.close()
    finally:
        if worker.poll() is None:
            worker.terminate();worker.wait(timeout=10)


def test_cache_restore_caps_legacy_sources_and_preserves_discovery_purpose(research):
    from backend.services.procurement_research import now
    service, run, provider, _ = research
    sources = [{'url':f'https://example.edu.cn/{i}','source_type':'unknown','body_status':'fetched','body_text':'目录'} for i in range(8)]
    key='search-cache-'+hashlib.sha256(b'bounded|discovery|procurement-multisource-v4').hexdigest()
    service.save({'id':key,'task_id':run['task_id'],'created_at':now(),'sources':sources,'cache_version':4})
    service.query(run,'bounded','discovery')
    assert len(run['sources'])==5
    assert all(s['purpose']=='discovery' for s in run['sources'])
    assert not provider.reads
    unrelated={'url':'https://example.edu.cn/guide','source_type':'unknown','body_status':'fetched','body_text':'会议操作指南 https://files.example/guide.pdf'}
    service.fetch_attachments(run,unrelated)
    assert not provider.reads


def test_discovery_recovers_literal_identity_but_not_invented_specifications(research, monkeypatch):
    # 末三条断言读的是核验产物（``status`` / ``matrix``），只有**批量查价**那条路会建。
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    url = 'https://vendor.example/product'
    source = {'url':url,'body_status':'fetched','body_text':'乙厂产品\nQ20 技术说明\n容量120 ml','source_type':'unknown','purpose':'discovery'}
    run['sources']=[source]
    model_output={'candidates':[{'brand':'乙厂','model':'Q20','url':url,'excerpt':'Q20 容量999ml 编造合并引文','configuration':'容量999ml'}]}
    monkeypatch.setattr(service,'model',lambda *args:copy.deepcopy(model_output))
    monkeypatch.setattr(service,'query',lambda *args, **kwargs:None)
    monkeypatch.setattr(service,'verify_publishers',lambda *args:None)
    monkeypatch.setattr(service,'prices',lambda *args:[])
    service.alternatives(run)
    lead=run['discovered_candidates'][0]
    assert lead['excerpt']=='Q20 技术说明' and lead['configuration']==''
    assert run['alternatives'][0]['status']=='needs_review'
    assert run['alternatives'][0]['matrix'][0]['observed'] is None
    assert run['alternatives'][0]['matrix'][0]['raw_evidence'][0]['url'] == url
    assert '容量120 ml' in run['alternatives'][0]['matrix'][0]['raw_evidence'][0]['excerpt']
