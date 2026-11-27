"""Search challenge, result evidence, and resumable empty-fallback regressions."""
import base64

import pytest

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.search_providers import (
    PublicWebSearchProvider, ProviderConfigurationError,
)
from backend.services.supplemental_research import eligibility
from tests.test_reliability_development import research


def test_bing_snippet_stays_with_its_result_and_wrapper_decodes_without_fetch():
    url = 'https://buyer.edu.cn/award?q=1&item=2'
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    html = f'''<li class="b_algo"><h2><a href="https://bing.com/ck/a?u=a1{encoded}">采购结果</a></h2>
    <div><p>型号<em></em>，成交1200元</p></div></li>
    <li class="b_algo"><h2><a href="https://other.example/">其他</a></h2><p>Q20 999元</p></li>'''
    rows = PublicWebSearchProvider._parse_results(html.encode(), 5)
    assert rows[0]['url'] == url and rows[0]['snippet'] == '型号Q10，成交1200元'
    assert '999' not in rows[0]['snippet'] and 'Q20' in rows[1]['snippet']


def test_empty_fallback_after_challenge_can_retry_and_reuses_success(research):
    service, run, _, _ = research
    class Recovering:
        name = 'recovering'
        blocked = True
        def search(self, query):
            if self.blocked:
                raise ProviderConfigurationError('challenge')
            return [SearchResult('https://buyer.edu.cn/award', 'Q10成交公告')]
    class Empty:
        name = 'empty'
        def search(self, query): return []
    provider = Recovering()
    before_model = run['usage']['model']
    service.search.providers = [provider, Empty()]
    service.fetch_source = lambda run, source: {**source, 'body_status': 'fetched', 'body_text': 'Q10成交公告', 'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    query = 'Q10 retry access'
    service.query(run, query, 'tender', target={'model': 'Q10'})
    assert query not in run['completed_queries'] and query in run['retryable_queries']
    assert run['provider_failure_counts']['recovering'] == 1
    service.recover_sources(run)
    assert run['unresolved_source_errors']
    provider.blocked = False
    # The first transient failure is retried on the next query; quarantine is
    # only entered after three consecutive failures.  Clear any persisted
    # marker when this fixture is reused with a later strategy version.
    run.pop('unavailable_search_providers', None)
    service.query(run, query, 'tender', target={'model': 'Q10'})
    assert query in run['completed_queries'] and query not in run['retryable_queries']
    assert run['provider_failure_counts']['recovering'] == 0
    usage = run['usage'].copy()
    service.query(run, query, 'tender', target={'model': 'Q10'})
    assert run['usage'] == usage and run['usage']['model'] == before_model


def test_fetch_only_shared_provider_does_not_spend_search_budget(research):
    service, run, _, _ = research

    class FetchOnly:
        name = 'fetch-only'

        def supports(self, _query):
            return False

        def search(self, _query):  # pragma: no cover - must never be called
            raise AssertionError('fetch-only provider was used for discovery')

        def fetch(self, _url, _timeout):
            return 'body'

    service.search.providers = [FetchOnly()]
    before = run['usage']['search']
    with pytest.raises(ValueError, match='搜索接口失败'):
        service.query(run, 'Q10 no discovery provider', 'tender', target={'model': 'Q10'})
    assert run['usage']['search'] == before
    assert run['unresolved_source_errors']


def test_explicit_next_round_reopens_quarantined_provider(research):
    service, run, _, _ = research
    run['unavailable_search_providers'] = ['baidu']
    run['provider_failure_counts'] = {'baidu': 3}
    service.save(run)

    class Finished:
        def done(self):
            return True

    class NoopPool:
        def submit(self, *_args, **_kwargs):
            return Finished()

    # Resume is asynchronous in production. Replace only the executor here so
    # the persisted next-round state can be asserted deterministically.
    service.futures[run['id']] = Finished()
    original_pool = service.pool
    service.pool = NoopPool()
    try:
        resumed = service.resume(run['task_id'], run['id'])
    finally:
        service.pool = original_pool

    assert 'unavailable_search_providers' not in resumed
    assert 'provider_failure_counts' not in resumed


def test_supplier_revision_retains_real_research_provenance_for_supplement(research):
    service, run, _, _ = research
    assert eligibility(service.repo, run)['allowed']
    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})
    assert updated['started_at'] == run['started_at'] and updated['finished_at'] == run['finished_at']
    assert eligibility(service.repo, updated)['allowed']
    # 教育／科研交易平台是第四个计数位，省级政采平台按型号直查是第五个；
    # 0 = 这一轮没有为它们发过请求。
    assert updated['usage'] == {'search': 0, 'fetch': 0, 'model': 0,
                                'education_platform': 0, 'province_award': 0}


def test_supplier_revision_does_not_erase_unresolved_search_failures(research):
    service, run, _, _ = research
    run['unresolved_source_errors'] = True
    run['retryable_queries'] = ['Q10 blocked']
    service.save(run)
    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})
    assert updated['retryable_queries'] == ['Q10 blocked']
    assert not eligibility(service.repo, updated)['allowed']


def test_discovery_topic_collision_never_spends_body_or_model_budget(research):
    service, run, _, _ = research
    run['baseline']['category'] = '生化培养箱'
    class Provider:
        name = 'collision'
        def search(self, query):
            return [SearchResult('https://example.org/pregnancy', '生化妊娠是什么原因'),
                    SearchResult('https://vendor.example/product', '实验室设备 Q20', snippet='恒温孵育箱 技术参数')]
    service.search.providers = [Provider()]
    reads = []
    service.fetch_source = lambda run, source: reads.append(source['url']) or {**source, 'body_status': 'fetched', 'body_text': 'Q20技术参数', 'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    service.query(run, 'new candidate discovery', 'discovery')
    assert reads == ['https://vendor.example/product']
    assert any(s['url'].endswith('/pregnancy') for s in run['skipped_search_results'])


def test_legacy_empty_search_does_not_hide_a_current_access_failure(research):
    import hashlib
    from backend.services.procurement_research import now
    service, run, _, _ = research
    text = 'legacy blocked query'
    key = 'search-cache-' + hashlib.sha256((text + '|tender').encode()).hexdigest()
    service.save({'id': key, 'task_id': run['task_id'], 'created_at': now(), 'cache_version': 4, 'sources': []})
    class Blocked:
        name = 'blocked'
        def search(self, query): raise ProviderConfigurationError('challenge')
    service.search.providers = [Blocked()]
    with pytest.raises(ValueError, match='搜索接口失败'):
        service.query(run, text, 'tender')
    assert text not in run['completed_queries']
