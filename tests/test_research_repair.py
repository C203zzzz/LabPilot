from urllib.error import HTTPError

from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.services.procurement_rules import grounded_parameter
from backend.services.source_identity import verify_publisher
from backend.services.research_planning import triage, discovery_queries
from tests.test_reliability_development import research


def test_procurement_search_covers_buyers_agents_and_preserves_suffix():
    from backend.services.research_planning import procurement_queries
    queries = procurement_queries({'brand': '星海', 'model': 'AX220S'})
    assert len(queries) == 6
    assert ('"AX220S" 成交', 'tender') in queries
    assert all('AX220S' in q or 'AX 220S' in q for q, _ in queries)
    assert any('AX220S' in q for q, _ in queries)
    assert any('site:edu.cn' in q for q, _ in queries)
    assert any('科研院所' in q for q, _ in queries)
    assert 'site:edu.cn' in queries[0][0]
    assert any('site:gov.cn' in q for q, _ in queries)
    assert any('招标代理' in q for q, _ in queries)
    assert not any('2026 2025 2024' in q for q, _ in queries)


def test_alternative_prices_apply_buyer_queries_to_candidate_not_baseline(research):
    service, run, _, _ = research
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    queries, targets = [], []
    def query(run, text, purpose, target=None):
        queries.append(text)
        run['usage']['search'] += 1
    service.query = query
    service.prices = lambda run, target: targets.append(target['model']) or []
    service.trace_procurement = lambda run, target, reserve_search: targets.append(target['model'])
    candidate = {'brand': '另一品牌', 'model': 'BX90S'}
    assert service.alternative_prices(run, candidate) == []
    assert len(queries) == 6 and all('BX90S' in q or 'BX 90S' in q for q in queries)
    assert any('site:edu.cn' in q for q in queries)
    assert any('科研院所' in q for q in queries) and any('招标代理' in q for q in queries)
    assert set(targets) == {'BX90S'}
    run['usage']['search'] = run['limits']['search'] - 2
    service.alternative_prices(run, candidate)
    assert len(queries) == 6 and candidate['price_search_gap']


def test_procurement_relevance_skips_short_model_noise_but_keeps_notices():
    from backend.services.research_planning import procurement_relevant
    target = {'model': 'BX-20M', 'category': '离心机'}
    assert not procurement_relevant({'url': 'https://steel.example/20m', 'title': 'BX steel plate 20m'}, target)
    assert procurement_relevant({'title': '大学仪器采购成交公告', 'url': 'https://lab.example.edu.cn/award'}, target)
    assert procurement_relevant({'title': 'BX-20M 参数', 'url': 'https://vendor.example/model'}, target)


def test_procurement_model_plans_require_real_source_anchor():
    from backend.services.research_planning import procurement_followups
    sources = [{'url': 'https://lead.example/a', 'body_status': 'fetched', 'body_text': '星海大学2025年实验仪器采购项目 AX 220S 中标公告'}]
    valid = {'query': '星海大学2025年实验仪器采购项目 中标公告', 'anchor': '星海大学2025年实验仪器采购项目', 'source_url': sources[0]['url']}
    rows = [valid, valid, {**valid, 'query': '不存在的医院 采购', 'anchor': '不存在的医院'}, {**valid, 'source_url': 'https://invented.example'}]
    assert len(procurement_followups({'queries': rows}, sources, [])) == 1
    assert not procurement_followups({'queries': rows}, sources, [valid['query']])
    institution = {**valid, 'anchor': '星海大学', 'query': '星海大学 仪器 中标'}
    query = procurement_followups({'queries': [institution]}, sources, [], model='AX220S')[0]['query']
    assert 'AX 220S' in query
    assert not procurement_followups({'queries': [institution]}, sources, [query], model='AX220S')
    repaired = procurement_followups({'queries': [{**valid, 'query': '星海大学 仪器采购 中标'}]}, sources, [], model='AX220S')
    assert valid['anchor'] in repaired[0]['query']


def test_procurement_trace_uses_project_model_and_reserves_discovery_budget(research):
    service, run, _, _ = research
    # Exercise this stage with a fresh per-run budget, independent of the
    # fixture's already-completed discovery workflow (no real ledger involved).
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['sources'] = [{'url': 'https://lead.example/a', 'body_status': 'fetched', 'body_text': '星海大学采购项目 Q10 成交公告'}]
    calls = []
    class Model:
        def analyze(self, **kwargs):
            calls.append(kwargs)
            return {'queries': [{'query': '星海大学采购项目 中标公告', 'anchor': '星海大学采购项目', 'source_url': 'https://lead.example/a'}]}
    service.client_factory = Model
    before = dict(run['usage'])
    service.trace_procurement(run)
    assert calls and '科研院所' in calls[0]['system_prompt'] and '招标代理' in calls[0]['system_prompt']
    assert run['usage']['search'] == before['search'] + 1 and run['usage']['model'] == before['model'] + 1
    assert run['procurement_query_plans'][0]['anchor'] == '星海大学采购项目'
    run['usage']['search'] = run['limits']['search'] - 5
    service.trace_procurement(run)
    assert len(calls) == 1


def test_aggregator_generic_downloads_do_not_consume_attachment_budget():
    from backend.services.research_sources import attachments
    source = {'url': 'https://index.example/product', 'source_type': 'unknown',
              'body_text': 'AX220S 中标记录\n产品介绍书 [https://index.example/service.pdf]\n报价单 [https://index.example/pricing.pdf]\n采购合同 [https://files.example/contract.pdf]'}
    assert attachments(source, model='AX220S') == ['https://files.example/contract.pdf']
    assert len(attachments({**source, 'source_type': 'tender_document'}, model='AX220S')) == 3


def test_public_document_cookie_redirect_is_request_local(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from backend.services import research_sources
    # Local server exists only in this test; production keeps public-IP checks.
    monkeypatch.setattr(research_sources, 'network_url', lambda url: url)
    visits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cookie = self.headers.get('Cookie', '')
            visits.append(cookie)
            if 'document_session=yes' not in cookie:
                self.send_response(302)
                self.send_header('Location', '/document.pdf')
                self.send_header('Set-Cookie', 'document_session=yes; Path=/')
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.end_headers()
                self.wfile.write(b'%PDF-public-document')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/document.pdf'
        provider = PublicWebSearchProvider()
        assert provider.fetch_raw(url, 2)[0] == b'%PDF-public-document'
        assert provider.fetch_raw(url, 2)[0] == b'%PDF-public-document'
        assert visits == ['', 'document_session=yes', '', 'document_session=yes']
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_html_model_columns_keep_blanks_spans_and_suffixes():
    html = '''<table><tr><th>型号</th><th></th><th>AX10</th><th>AX10S</th></tr>
    <tr><td>容量</td><td></td><td>80 ml</td><td>160 ml</td></tr>
    <tr><td>噪声</td><td colspan="2">50 dB</td><td>60 dB</td></tr></table>'''
    text = PublicWebSearchProvider._clean_html(html)
    assert 'AX10 容量 80 ml' in text and 'AX10S 容量 160 ml' in text
    assert 'AX10 噪声 50 dB' in text and 'AX10S 噪声 60 dB' in text
    assert 'AX10 容量 160 ml' not in text
    source = {'url': 'https://vendor.example/a', 'body_status': 'fetched', 'source_type': 'manufacturer_website', 'body_text': text}
    line = next(l for l in text.splitlines() if 'AX10S 容量 160 ml' in l)
    assert grounded_parameter({'excerpt': line, 'value': 160, 'unit': 'ml'}, source, 'AX10S', '容量')
    assert not grounded_parameter({'excerpt': text, 'value': 160, 'unit': 'ml'}, source, 'AX10', '容量')


def test_neighbor_model_cannot_lend_parameter():
    text = 'AX10 产品介绍\nAX20 容量160 ml'
    source = {'body_text': text, 'body_status': 'fetched', 'source_type': 'manufacturer_website'}
    assert not grounded_parameter({'excerpt': text, 'value': 160, 'unit': 'ml'}, source, 'AX10', '容量')
    for first, second in [('3H24RI', '3H20RI'), ('Q10', 'Q10S')]:
        text = f'{first} 产品介绍\n{second} 容量160 ml'
        assert not grounded_parameter({'excerpt': text, 'value': 160, 'unit': 'ml'}, {**source, 'body_text': text}, first, '容量')


def test_institution_structured_identity_requires_single_matching_company():
    candidate = {'url': 'https://www.vendor.example/product', 'body_text': '星海科学仪器有限公司 产品目录'}
    source = {'url': 'https://lab.example.edu.cn/makers', 'body_status': 'fetched',
              'body_text': '企业名称\n星海科学仪器有限公司\n企业网址\nhttps://www.vendor.example'}
    assert verify_publisher(candidate, [source], '星海')
    assert not verify_publisher({**candidate, 'body_text': '不同公司'}, [source], '星海')
    assert not verify_publisher(candidate, [{**source, 'body_text': source['body_text'].replace('企业网址', '代理商网址')}], '星海')
    assert not verify_publisher(candidate, [{**source, 'body_text': source['body_text'].replace('企业网址', '另一仪器有限公司\n企业网址')}], '星海')
    assert not verify_publisher(candidate, [{**source, 'body_text': source['body_text'] + '.evil.invalid'}], '星海')
    named = {**source, 'body_text': '合作单位 星海科学仪器有限公司 [https://www.vendor.example]'}
    assert verify_publisher(candidate, [named], '星海')
    assert not verify_publisher(candidate, [{**named, 'body_text': '供应商 ' + named['body_text']}], '星海')
    assert not verify_publisher(candidate, [{**source, 'body_text': '星海经销商官网 https://www.vendor.example'}], '星海')


def test_terminal_http_and_transient_failure_are_different(research):
    service, run, provider, _ = research
    def gone(*args):
        raise HTTPError(args[0], 410, 'gone', {}, None)
    provider.fetch = gone
    source = service.fetch_source(run, {'url': 'https://vendor.example/a'})
    assert source['body_status'] == 'unavailable'
    run['sources'] = [source]
    service.recover_sources(run)
    assert not run['unresolved_source_errors'] and run['unavailable_sources']
    def timeout(*args):
        raise TimeoutError()
    provider.fetch = timeout
    run['sources'] = [service.fetch_source(run, {'url': 'https://vendor.example/b'})]
    before = run['usage']['fetch']
    service.recover_sources(run)
    service.recover_sources(run)
    assert run['usage']['fetch'] == before + 1
    assert run['unresolved_source_errors']
    run['round'] += 1
    provider.fetch = lambda *args: 'AX10 容量100ml'
    service.recover_sources(run)
    assert not run['unresolved_source_errors'] and run['sources'][0]['body_status'] == 'fetched'


def test_triage_is_ranking_only_and_queries_keep_units():
    req = {'name': '转速精度', 'value': 10, 'unit': 'r/min', 'direction': 'max'}
    source = {'url': 'https://v.example', 'source_type': 'unknown', 'body_text': '[表格按列展开 行2列2] AX10 转速精度 50r/min'}
    c = {'model': 'AX10', 'url': source['url']}
    finding = triage(c, [source], [req])
    assert finding[0]['status'] == 'lead_below_requirement'
    assert 'status' not in c
    assert '10 r/min' in discovery_queries({'category': '离心机', 'parameters': [req]})[0]
    rotor = {'name': '角转子最高转速', 'conditions': '角转子', 'value': 22000, 'unit': 'r/min', 'direction': 'min'}
    assert not triage(c, [{**source, 'body_text': '[表格按列展开 行2列2] AX10 水平转子最高转速 4000r/min'}], [rotor])


def test_exact_model_cache_reuses_body_without_mutating_saved_result(research):
    service, run, _, _ = research
    class Client:
        calls = 0
        def analyze(self, **kwargs):
            self.calls += 1
            return {'candidates': [{'model': 'Q10'}]}
    client = Client()
    service.client_factory = lambda: client
    first = service.model(run, 'exact prompt', {'body': 'original'})
    first['candidates'].clear()
    run.pop('model_results', None)
    second = service.model(run, 'exact prompt', {'body': 'original'})
    assert second['candidates'] and client.calls == 1
    service.model(run, 'exact prompt', {'body': 'changed'})
    assert client.calls == 2


def test_company_biography_is_not_procurement_publication():
    from backend.services.research_sources import is_tender
    base = {'url': 'https://institute.example.edu.cn/company', 'body_status': 'fetched'}
    assert not is_tender({**base, 'body_text': '公司守合同重信用，产品在各地采购项目多次中标。服务手册 link.pdf'})
    assert is_tender({**base, 'body_text': '2026年仪器成交公告\nQ10 单价1000元'})
    assert not is_tender({**base, 'url': 'https://dealer.example/', 'body_text': '成交公告'})


def test_scanned_web_pdf_reuses_hash_identical_uploaded_text(research):
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    import hashlib
    service, run, provider, _ = research
    raw = b'%PDF-identical-scanned-fixture'
    provider.fetch_raw = lambda *args: (raw, 'application/pdf')
    provider.decode_body = lambda *args: (_ for _ in ()).throw(ValueError('no native text'))
    digest = hashlib.sha256(raw).hexdigest()
    service.repo.put('documents', 'd', {'id': 'd', 'sha256': digest, 'extracted_text': 'Q10 真实本地OCR参数'}, task_id=run['task_id'])
    s = service.fetch_source(run, {'url': 'https://vendor.example/scanned.pdf'})
    assert s['body_status'] == 'fetched' and s['body_text'] == 'Q10 真实本地OCR参数'
    assert s['text_provenance']['sha256'] == digest and s['source_type'] == 'unknown'


def test_document_cache_rebinds_sources_and_invalidates_changed_context(tmp_path):
    import json
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.analysis_checkpoint import AnalysisCheckpoint
    repo = SQLiteRepository(tmp_path/'cache.db')
    for task, doc in [('a', 'doc-a'), ('b', 'doc-b')]:
        repo.put('documents', doc, {'id': doc, 'sha256': 'f'*64}, task_id=task)
    client = object()
    a, b = AnalysisCheckpoint(repo, 'a'), AnalysisCheckpoint(repo, 'b')
    def request(doc, text='原文'):
        return {'system_prompt': '提取', 'user_prompt': '采购场景：实验\n' + json.dumps({'document_id': doc, 'text': text})}
    key = a.key(client, request('doc-a'))
    a.save(key, 'completed', result={'sources': [{'document_id': 'doc-a', 'excerpt': '原文'}]})
    hit = b.get(b.key(client, request('doc-b')))
    assert hit['result']['sources'][0]['document_id'] == 'doc-b'
    assert a.get(key)['result']['sources'][0]['document_id'] == 'doc-a'
    assert not b.get(b.key(client, request('doc-b', '修改原文')))
    assert not b.get(b.key(client, {**request('doc-b'), 'system_prompt': '新提取规则'}))
    repo.close()


def test_procurement_amount_cannot_come_from_neighbor_table_row():
    from backend.services.procurement_rules import validate_price
    body = '成交公告\nAX10 | 数量1台 | 单价1000元\nAX20 | 数量1台 | 单价800元'
    source = {'url': 'https://lab.example.edu.cn/award', 'body_status': 'fetched', 'source_type': 'tender_document', 'body_text': body}
    raw = {'model': 'AX10', 'kind': 'award', 'amount': 800, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'excerpt': body}
    assert not validate_price(raw, source, {'model': 'AX10'})
    assert validate_price({**raw, 'amount': 1000}, source, {'model': 'AX10'})['unit_price'] == 1000


def test_unavailable_import_is_a_missing_source_not_an_endless_retry(research):
    from backend.services.supplemental_research import import_leads, eligibility, report_id
    service, run, provider, _ = research
    provider.fetch = lambda url, timeout: (_ for _ in ()).throw(HTTPError(url, 404, 'gone', {}, None))
    payload = {'report_id': report_id(run), 'sources': [{'url': 'https://vendor.example/gone'}]}
    child = import_leads(service, run['task_id'], run['id'], payload)
    assert not child.get('supplement_retryable')
    gate = eligibility(service.repo, child)
    assert gate['allowed'] and any(g['kind']=='source_unavailable' for g in gate['gaps'])


def test_native_worker_failure_does_not_create_success_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from backend.services import structured_pdf
    pdf = tmp_path/'source.pdf'; pdf.write_bytes(b'%PDF-original')
    monkeypatch.setattr(
        structured_pdf.subprocess,
        'Popen',
        lambda *args, **kwargs: SimpleNamespace(pid=99999, poll=lambda: -1073741819, kill=lambda: None, wait=lambda timeout=None: -1073741819),
    )
    with pytest.raises(RuntimeError, match='子进程失败'):
        structured_pdf.parse_document(pdf, tmp_path/'cache')
    assert not list((tmp_path/'cache').glob('*.json'))
    assert pdf.read_bytes() == b'%PDF-original'


def test_single_dead_domain_is_distinguished_from_general_dns_outage(research):
    import socket
    service, run, provider, _ = research
    provider.fetch = lambda *args: (_ for _ in ()).throw(socket.gaierror(socket.EAI_NONAME, 'not found'))
    dead = service.fetch_source(run, {'url': 'https://missing.example/a'})
    run['sources'] = [dead]
    service.recover_sources(run)
    assert run['unresolved_source_errors']  # no evidence other hosts work
    run['round'] += 1
    run['sources'] += [{'url': f'https://ok{i}.example/', 'body_status': 'fetched'} for i in range(2)]
    service.recover_sources(run)
    assert not run['unresolved_source_errors'] and run['sources'][0]['body_status']=='unavailable'


def test_page_text_failure_preserves_all_pdf_pages(tmp_path, monkeypatch):
    import fitz
    from pypdf._page import PageObject
    from backend.services.document_ingestion import DocumentIngestionService
    from backend.services import structured_pdf
    pdf = fitz.open()
    for _ in range(3):
        pdf.new_page().insert_text((30, 30), 'AX100 technical brochure')
    path = tmp_path/'pages.pdf'; pdf.save(path); pdf.close()
    original = PageObject.extract_text
    count = [0]
    def fail_once(self, *args, **kwargs):
        count[0] += 1
        if count[0] == 2:
            raise MemoryError('simulated allocation failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(PageObject, 'extract_text', fail_once)
    monkeypatch.setattr(structured_pdf, 'parse_document', lambda *a, **k: {'regions': []})
    result = DocumentIngestionService(tmp_path/'files').ingest_file(path)
    assert [p.number for p in result.pages] == [1,2,3]
    assert result.pages[1].route == 'vision'
    assert any(w.code=='pdf_page_text_failed' and w.location=='page:2' for w in result.warnings)


def test_predecessor_mention_is_not_an_independent_product():
    from backend.services.deepseek_analysis import _candidate_has_only_non_instrument_evidence
    source = {'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': 'AX200作为AX100的升级款，提升容量'}
    evidence = {('d','page:1'): source['excerpt']}
    assert _candidate_has_only_non_instrument_evidence({'model':'AX100','product_role':'instrument'}, [source], evidence)
    assert not _candidate_has_only_non_instrument_evidence({'model':'AX200','product_role':'instrument'}, [source], evidence)
    direct = {**source,'excerpt':'AX100 主仪器容量100ml'}
    assert not _candidate_has_only_non_instrument_evidence({'model':'AX100','product_role':'instrument'}, [source,direct], evidence)
