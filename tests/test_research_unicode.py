from backend.infrastructure.unicode_text import repair_surrogates
from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_research import ProcurementResearch
from backend.services.official_search import OfficialSearchService


def test_unicode_pairs_preserve_symbol_and_lone_surrogate_remains_explicit():
    text, count = repair_surrogates('中文 \ud83d\ude00 20 L \ud800 test \udc01')
    assert text == '中文 😀 20 L � test �'
    assert count == 2
    assert text.encode('utf-8')
    assert repair_surrogates('正常 µ ± 20 L 😀') == ('正常 µ ± 20 L 😀', 0)


def test_external_pdf_surrogate_does_not_crash_persistence_or_destroy_snapshot(tmp_path):
    class Fetcher:
        def fetch(self, url, timeout):
            raise AssertionError('raw snapshot path should be used')
        def fetch_raw(self, url, timeout):
            return b'%PDF-original-untouched', 'application/pdf'
        def decode_body(self, payload, content_type, url):
            return 'Model ABC volume 20 L\ud800'
    repo = SQLiteRepository(tmp_path / 'app.sqlite3')
    service = ProcurementResearch(repo, OfficialSearchService(providers=[Fetcher()]), None)
    repo.put('tasks', 'task', {'id': 'task'})
    run = {'id': 'r', 'kind': 'research', 'task_id': 'task', 'status': 'running',
           'baseline': {'version': 1}, 'usage': {'fetch': 0}, 'limits': {'fetch': 2}}
    repo.put('procurement_records', 'r', run, task_id='task')
    source = service.fetch_source(run, {'url': 'https://example.org/a.pdf', 'source_type': 'unknown'})
    assert source['body_status'] == 'fetched'
    assert source['body_text'] == 'Model ABC volume 20 L�'
    assert source['text_warnings'][0]['count'] == 1
    snapshot = tmp_path / 'research-snapshots' / (source['snapshot']['sha256'] + '.bin')
    assert snapshot.read_bytes() == b'%PDF-original-untouched'
    run['sources'] = [source]
    service.persist(run)
    assert service.get('task', 'r')['sources'][0]['body_text'].encode('utf-8')
    cached = service.fetch_source(run, {'url': source['url'], 'source_type': 'unknown'})
    assert cached['cache_hit'] is True
    assert cached['text_warnings'] == source['text_warnings']
    service.close()


def test_unreadable_pdf_keeps_snapshot_and_explicit_partial_gap(tmp_path):
    class Fetcher:
        def fetch(self, url, timeout):
            raise AssertionError('raw snapshot path should be used')

        def fetch_raw(self, url, timeout):
            return b'%PDF-not-readable', 'application/pdf'

        def decode_body(self, payload, content_type, url):
            raise ValueError('PDF正文无法解析，请补充OCR')

    repo = SQLiteRepository(tmp_path / 'app.sqlite3')
    service = ProcurementResearch(repo, OfficialSearchService(providers=[Fetcher()]), None)
    repo.put('tasks', 'task', {'id': 'task'})
    run = {'id': 'r', 'kind': 'research', 'task_id': 'task', 'status': 'running',
           'baseline': {'version': 1}, 'usage': {'fetch': 0}, 'limits': {'fetch': 2}}
    repo.put('procurement_records', 'r', run, task_id='task')
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/item.pdf', 'source_type': 'unknown'})
    assert source['body_status'] == 'failed'
    assert source['failure_kind'] == 'parse_incomplete'
    assert source['parse_status'] == 'partial'
    assert '原始快照已保存' in source['gap_reason']
    assert source['snapshot']['size'] == len(b'%PDF-not-readable')
    service.close()
