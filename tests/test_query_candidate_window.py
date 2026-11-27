"""每查询候选窗口：窗口大小不变（discovery 5 / 其余 3），但「丢了多少」必须留痕（全离线）。

``procurement_research.query`` 有两处把「一条查询召回的候选」截成固定条数才送进正文
抓取：命中搜索缓存那一条路径（master ``:1885``）和真实检索那一条路径（master ``:2091``）。
这个窗口比 ``search`` / ``fetch`` 额度**更早**生效，而丢弃过去不留任何记录
（设计规范、设计规范）。

本文件钉三件事：

* **取值一个字没变**：非 discovery 是 3、discovery 是 5，两处魔法数字现在都走
  ``body_candidate_limit(purpose)``（``BODY_CANDIDATE_LIMITS``）；
* 每条查询都在 ``run['search_attempts']`` 里留下
  ``candidates_recalled / candidates_in_body / candidates_dropped / candidate_limit /
  candidate_origin``——没有它，台账 ``设计规范`` 第 67 行那句「先给丢弃留痕再评估」
  就没有可读的数字；
* 搜索缓存命中**本来一行记录都没有**，现在补一行 ``status='search_cache_hit'``：
  缓存路径同样会截断。

**放宽到统一 5 的那一档不在本文件里**：``设计规范`` 的对照实验在这个样本上没测出收益
（候选层多进 24 条、正文 60 对基线 62、价格 0 对 0、墙钟 +27.7 s、都是 ``fetch=72``
先顶格），所以窗口大小保持现状，只收留痕。

不联网：provider 只实现 ``search``（于是 ``prefetch_bodies`` 找不到 fetcher，
一个请求都不会发出去），正文读取由测试替身计数。
"""
import hashlib

from backend.domain.search import SearchResult
from backend.services.procurement_research import (BODY_CANDIDATE_LIMITS,
                                                   SEARCH_STRATEGY_VERSION,
                                                   body_candidate_limit, now)
from tests.test_reliability_development import research  # noqa: F401  这是 fixture

#: 一次查询在 ``search_attempts`` 上留下的候选窗口字段（本单新增的全部字段）。
WINDOW_FIELDS = {'candidates_recalled', 'candidates_in_body', 'candidates_dropped',
                 'candidate_limit', 'candidate_origin'}


def _results(count, path='award'):
    return [SearchResult(f'https://lab.edu.cn/{path}/{index}',
                         f'Q10 离心机采购成交公告 {index}') for index in range(count)]


def _provider(results):
    class Probe:
        name = 'probe'

        def search(self, query):
            return list(results)

    return Probe()


def _recording_fetch(read):
    def fetch_source(run, source):
        read.append(source['url'])
        return {**source, 'body_status': 'fetched',
                'body_text': 'Q10 离心机采购成交公告', 'source_type': 'unknown'}

    return fetch_source


def _budget(run, fetch=72):
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 8, 'fetch': fetch, 'model': 8}


def _cache_id(text, purpose):
    scope = f'|procurement-search-v{SEARCH_STRATEGY_VERSION}'
    return 'search-cache-' + hashlib.sha256((text + '|' + purpose + scope).encode()).hexdigest()


# --------------------------------------------------------------------------
# 窗口大小本身：有名字、按用途分档、取值没变
# --------------------------------------------------------------------------

def test_the_window_is_a_named_per_purpose_limit_with_unchanged_values():
    """硬要求：本单不引入任何行为改变，取值与改动前的内联三元表达式逐字相同。"""
    assert body_candidate_limit('discovery') == 5     # 原来就是 5
    assert body_candidate_limit('tender') == 3        # 原来就是 3
    assert body_candidate_limit('official') == 3
    assert body_candidate_limit('') == 3
    assert body_candidate_limit(None) == 3
    assert BODY_CANDIDATE_LIMITS == {'discovery': 5, 'default': 3}


# --------------------------------------------------------------------------
# 真实检索路径（master :2091）
# --------------------------------------------------------------------------

def test_a_query_with_six_candidates_sends_three_bodies_and_records_the_drop(research):
    """窗口是 3：第 4-6 条既不抓也不进 ``run['sources']``，但必须留下丢弃计数。"""
    service, run, _, _ = research
    read = []
    service.search.providers = [_provider(_results(6))]
    service.fetch_source = _recording_fetch(read)
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, 'Q10 离心机 成交', 'tender', target={'model': 'Q10', 'category': '离心机'})

    assert read == [f'https://lab.edu.cn/award/{index}' for index in range(3)], read
    assert not any(source['url'].endswith(('/award/3', '/award/4', '/award/5'))
                   for source in run['sources'])
    trace = run['search_attempts'][-1]
    assert trace['query'] == 'Q10 离心机 成交'
    assert (trace['candidates_recalled'], trace['candidates_in_body'],
            trace['candidates_dropped']) == (6, 3, 3)
    assert trace['candidate_limit'] == 3
    assert trace['candidate_origin'] == 'provider'
    assert WINDOW_FIELDS <= set(trace), sorted(trace)


def test_a_query_that_recalls_no_more_than_the_window_drops_nothing(research):
    """计数是诚实的：没有截断就不能报出「丢弃」。"""
    service, run, _, _ = research
    read = []
    service.search.providers = [_provider(_results(3))]
    service.fetch_source = _recording_fetch(read)
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, 'Q10 离心机 成交', 'tender', target={'model': 'Q10', 'category': '离心机'})

    assert len(read) == 3
    trace = run['search_attempts'][-1]
    assert (trace['candidates_recalled'], trace['candidates_in_body'],
            trace['candidates_dropped']) == (3, 3, 0)
    assert trace['candidate_limit'] == 3


def test_a_discovery_query_keeps_its_five_candidate_window(research):
    """discovery 是 5：收进常量表之后不能变小，也不能被默认档的 3 吃掉。"""
    service, run, _, _ = research
    read = []
    service.search.providers = [_provider(_results(7))]
    service.fetch_source = _recording_fetch(read)
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, '离心机 技术参数', 'discovery')

    assert len(read) == 5
    trace = run['search_attempts'][-1]
    assert (trace['candidates_recalled'], trace['candidates_in_body'],
            trace['candidates_dropped']) == (7, 5, 2)
    assert trace['candidate_limit'] == 5


# --------------------------------------------------------------------------
# 搜索缓存路径（master :1885）
# --------------------------------------------------------------------------

def test_the_search_cache_path_uses_the_same_window_and_leaves_a_trace(research):
    """缓存命中不消耗任何通道调用，这条查询本来一行记录都没有。"""
    service, run, _, _ = research
    text, purpose = 'Q10 离心机 缓存命中', 'tender'
    service.save({'id': _cache_id(text, purpose), 'task_id': run['task_id'],
                  'kind': 'search_cache', 'created_at': now(),
                  'cache_version': SEARCH_STRATEGY_VERSION,
                  'sources': [{'url': f'https://lab.edu.cn/cache/{index}',
                               'title': f'Q10 离心机采购成交公告 {index}',
                               'source_type': 'unknown'} for index in range(6)]})
    read = []
    service.search.providers = []
    service.fetch_source = _recording_fetch(read)
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, text, purpose, target={'model': 'Q10', 'category': '离心机'})

    assert read == [f'https://lab.edu.cn/cache/{index}' for index in range(3)], read
    assert run['usage']['search'] == 0, '缓存命中不应该花检索额度'
    trace = run['search_attempts'][-1]
    assert trace['query'] == text and trace['status'] == 'search_cache_hit'
    assert (trace['candidates_recalled'], trace['candidates_in_body'],
            trace['candidates_dropped']) == (6, 3, 3)
    assert trace['candidate_limit'] == 3
    assert trace['candidate_origin'] == 'search_cache'
    assert WINDOW_FIELDS <= set(trace), sorted(trace)


def test_the_search_cache_path_also_honours_the_discovery_window(research):
    """缓存路径同样按用途分档：discovery 命中缓存时仍是 5，不是默认的 3。"""
    service, run, _, _ = research
    text, purpose = '离心机 技术参数 缓存命中', 'discovery'
    service.save({'id': _cache_id(text, purpose), 'task_id': run['task_id'],
                  'kind': 'search_cache', 'created_at': now(),
                  'cache_version': SEARCH_STRATEGY_VERSION,
                  'sources': [{'url': f'https://lab.edu.cn/dcache/{index}',
                               'title': f'离心机采购成交公告 {index}',
                               'source_type': 'unknown'} for index in range(8)]})
    read = []
    service.search.providers = []
    service.fetch_source = _recording_fetch(read)
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, text, purpose)

    assert read == [f'https://lab.edu.cn/dcache/{index}' for index in range(5)], read
    trace = run['search_attempts'][-1]
    assert (trace['candidates_recalled'], trace['candidates_in_body'],
            trace['candidates_dropped']) == (8, 5, 3)
    assert trace['candidate_limit'] == 5
    assert trace['candidate_origin'] == 'search_cache'


def test_a_cache_retry_of_the_same_query_does_not_rewrite_the_first_trace(research):
    """同文本重试（第一条正文失败后走缓存）时，两条记录各说各的这次窗口。"""
    service, run, _, _ = research
    text = 'Q10 离心机 正文失败'
    read = []
    service.search.providers = [_provider(_results(6))]
    service.fetch_source = lambda run, source: {**source, 'body_status': 'failed',
                                                'source_type': 'unknown'}
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, text, 'tender', target={'model': 'Q10', 'category': '离心机'})
    first_rows = [row for row in run['search_attempts'] if row.get('query') == text]
    assert run['unresolved_source_errors']
    assert [row['candidate_origin'] for row in first_rows] == ['provider']
    assert (first_rows[0]['candidates_recalled'], first_rows[0]['candidates_in_body']) == (6, 3)

    service.fetch_source = _recording_fetch(read)
    service.query(run, text, 'tender', target={'model': 'Q10', 'category': '离心机'})

    assert len(read) == 3, read
    rows = [row for row in run['search_attempts'] if row.get('query') == text]
    assert len(rows) == 2, rows
    first, second = rows
    assert [first['candidate_origin'], second['candidate_origin']] == ['provider', 'search_cache']
    assert (first['candidates_recalled'], first['candidates_in_body']) == (6, 3)
    # 搜索缓存存的是上一次**进过正文窗口**的那 3 条（``query_sources``），不是原始
    # 召回集合。所以缓存重试的召回数就是 3、丢弃 0：3 条窗口之外那批候选，
    # 靠这条路是找不回来的，只能重新检索。
    assert (second['candidates_recalled'], second['candidates_in_body'],
            second['candidates_dropped']) == (3, 3, 0)
    assert second['status'] == 'search_cache_hit'


def test_a_failed_query_writes_no_window_trace(research):
    """所有通道都失败时这条查询什么都没进正文，不该凭空多出一行空窗口。"""
    import pytest

    from backend.infrastructure.search_providers import ProviderConfigurationError

    service, run, _, _ = research
    text = 'Q10 离心机 失败'

    class Blocked:
        name = 'blocked'

        def search(self, query):
            raise ProviderConfigurationError('搜索服务不可用')

    service.search.providers = [Blocked()]
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 8, 'fetch': 8, 'model': 8}

    with pytest.raises(ValueError, match='搜索接口失败'):
        service.query(run, text, 'tender', target={'model': 'Q10'})

    rows = [row for row in run['search_attempts'] if row.get('query') == text]
    assert [row['status'] for row in rows] == ['failed'], '失败本身仍要留痕'
    assert all('candidate_origin' not in row for row in rows)


# --------------------------------------------------------------------------
# persist 时机：额度顶格会抛 ResearchStopped，留痕必须已经落库
# --------------------------------------------------------------------------

def test_the_window_trace_survives_a_fetch_budget_stop(research):
    """``fetch`` 额度当场为零时正文一条不读，但窗口账必须已经写进库里。

    那正是最需要这条记录的一次：``设计规范`` 的实测就是「抓取额度先被顶格」，
    如果留痕只在查询成功收尾时写，被打断的那条查询恰恰会没有账。
    """
    import pytest

    from backend.services.procurement_research import ResearchStopped

    service, run, _, _ = research
    text = 'Q10 离心机 顶到额度'
    read = []

    def fetch_source(run, source):
        service.consume(run, 'fetch')  # 真实路径里这一步在 fetch_source 内部
        read.append(source['url'])
        return {**source, 'body_status': 'fetched',
                'body_text': 'Q10 离心机采购成交公告', 'source_type': 'unknown'}

    service.search.providers = [_provider(_results(6))]
    service.fetch_source = fetch_source
    service.fetch_attachments = lambda *args: None
    run['usage'] = {'search': 0, 'fetch': 0, 'model': 0}
    run['limits'] = {'search': 8, 'fetch': 0, 'model': 8}   # 正文额度当场就没有了

    with pytest.raises(ResearchStopped):
        service.query(run, text, 'tender', target={'model': 'Q10', 'category': '离心机'})

    assert read == [], '额度为 0 时一条正文都不该读'
    stored = service.get(run['task_id'], run['id'])
    rows = [row for row in stored['search_attempts'] if row.get('query') == text]
    assert len(rows) == 1, rows
    assert (rows[0]['candidates_recalled'], rows[0]['candidates_in_body'],
            rows[0]['candidates_dropped']) == (6, 3, 3), '被打断的查询也要留下窗口账'
    assert rows[0]['candidate_limit'] == 3


def test_admitted_candidates_spend_one_fetch_unit_each(research):
    """窗口里的 3 条都走网络时，真实消费是 3 个 fetch 单位（旧窗口也是 3）。"""
    service, run, _, _ = research
    read = []

    def fetch_source(run, source):
        service.consume(run, 'fetch')
        read.append(source['url'])
        return {**source, 'body_status': 'fetched',
                'body_text': 'Q10 离心机采购成交公告', 'source_type': 'unknown'}

    service.search.providers = [_provider(_results(6))]
    service.fetch_source = fetch_source
    service.fetch_attachments = lambda *args: None
    _budget(run)

    service.query(run, 'Q10 离心机 成交额度', 'tender', target={'model': 'Q10', 'category': '离心机'})

    assert len(read) == 3
    assert run['usage']['fetch'] == 3
