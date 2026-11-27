"""候选详情抓取在展示地址脱敏后仍沿用原始地址。"""

from backend.services.procurement_research import ProcurementResearch


def test_detail_fetch_uses_original_url_when_source_url_is_masked(monkeypatch):
    service = object.__new__(ProcurementResearch)
    calls = []

    def fake_fetch_source(run, detail):
        calls.append((detail['url'], detail['parent_url']))
        return {**detail, 'body_status': 'fetched', 'body_text': 'B10 恒温水浴锅\n品牌 赛默飞'}

    monkeypatch.setattr(service, 'fetch_source', fake_fetch_source)
    run = {'usage': {'fetch': 0}, 'limits': {'fetch': 5}, 'sources': [],
           'detail_page_sources': [], 'gaps': []}
    candidate = {'model': 'B10', 'url': '#', 'original_url': 'https://listing.example/waterbath',
                 'excerpt': 'B10 恒温水浴锅'}
    sources = [{'url': '#', 'original_url': 'https://listing.example/waterbath',
                'body_status': 'fetched',
                'body_text': 'B10 恒温水浴锅 [https://detail.example/b10]'}]

    ProcurementResearch.fetch_candidate_detail_pages(service, run, [candidate], sources)

    assert calls == [('https://detail.example/b10', 'https://listing.example/waterbath')]
    assert candidate['detail_page']['status'] == 'fetched'
