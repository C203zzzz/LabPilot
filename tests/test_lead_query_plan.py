"""查询计划：从线索派生查询式（设计规范 对话 3 第 1 条）。

provider 用假的：2a 还没把 `buyerName` / `projectId` / 时间窗接进
`backend/infrastructure/search_providers.py`，所以这里只验证"生成了哪些查询式、
参数对不对、时间窗是不是每条都带"，不验证 CCGP 是否真的认这些参数。
"""
from datetime import date, timedelta

import pytest

from backend.domain.search import SearchQuery, SearchResult, SourceType
from backend.services.lead_alignment import LeadProfile
from backend.services.official_search import OfficialSearchService, queries_from_lead

KUCHA_LEAD = LeadProfile(
    buyer="库车市农业农村局",
    supplier="新疆鑫华力科析科技有限公司",
    project_id="kcs2025-WT009",
    brand="知楚",
    model="1500A2",
    category="生物安全柜",
    date="2025-02-18",
)

HUBEI_URL = "https://xxgk.hubu.edu.cn/info/1269/12240.htm"


class RecordingProvider:
    """不认新参数的 provider：只记下收到的查询对象，模拟 2a 合并前的通道。"""

    name = "recording"

    def __init__(self, results=None):
        self.queries = []
        self.results = results or {}

    def supports(self, query):
        return True

    def search(self, query):
        self.queries.append(query)
        return list(self.results.get(query.text, []))

    def fetch(self, url, timeout):
        raise IOError("body not found in fixture")


def test_query_plan_priority_and_parameters():
    queries = queries_from_lead(KUCHA_LEAD)

    assert [q.text for q in queries] == [
        "kcs2025-WT009",     # 1 project_id 精确（kw 就是项目编号）
        "生物安全柜",         # 2 buyer_name + 品类词
        "1500A2",            # 3 buyer_name + 型号
        "新疆鑫华力科析科技有限公司",  # 4 供应商名（CCGP 没有卖方参数，只能当 kw）
        "库车市农业农村局",    # 5 buyer_name 单独
        "1500A2 生物安全柜",  # 6 型号 + 品类词兜底
    ]
    assert [q.buyer_name for q in queries] == [
        None, "库车市农业农村局", "库车市农业农村局", None, "库车市农业农村局", None,
    ]
    assert [q.supplier_name for q in queries] == [
        None, None, None, "新疆鑫华力科析科技有限公司", None, None,
    ]
    assert [q.project_id for q in queries] == ["kcs2025-WT009"] + [None] * 5
    assert all(q.purpose == "tender" for q in queries)
    # 只有"型号"那两步带 model/brand：CCGP 的 ladder 一旦拿到 brand/model 就把它当
    # 关键词、把 text 丢掉（真机实测），全步继承会让品类词/供应商名/买方名这三条视角
    # 在通道上永远不会真的被搜到。
    assert [q.model for q in queries] == [None, None, "1500A2", None, None, "1500A2"]
    assert [q.brand for q in queries] == [None, None, None, None, None, "知楚"]


def test_every_query_carries_an_explicit_window_derived_from_the_lead():
    queries = queries_from_lead(KUCHA_LEAD)
    assert queries
    for query in queries:
        assert query.window_start == "2024-02-19", "线索日期往前一年，不是固定的一年窗口"
        assert query.window_end == "2025-02-18"
        assert query.window_start <= query.window_end
    # 366 天以内：设计规范 事实 #5 说超过约 366 天 CCGP 静默返回 0 条
    span = date.fromisoformat(queries[0].window_end) - date.fromisoformat(queries[0].window_start)
    assert span.days <= 366


def test_window_falls_back_to_today_when_the_lead_has_no_date():
    lead = LeadProfile(buyer="某单位", model="AAA-1", category="培养箱")
    queries = queries_from_lead(lead)
    today = date.today()
    for query in queries:
        assert query.window_end == today.isoformat()
        assert query.window_start == (today - timedelta(days=365)).isoformat()


def test_explicit_wide_window_is_split_into_366_day_spans():
    lead = LeadProfile(buyer="某单位", model="AAA-1", category="培养箱",
                       window_start="2022-01-01", window_end="2025-01-01")
    queries = queries_from_lead(lead)
    spans = sorted({(q.window_start, q.window_end) for q in queries})
    assert len(spans) >= 2, "宽窗口必须拆成多段，否则 CCGP 静默返回 0 条"
    for start, end in spans:
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days <= 366
    # 最近的一段排在最前
    assert queries[0].window_end == "2025-01-01"


def test_plan_shrinks_when_the_lead_is_thin():
    lead = LeadProfile(buyer="库车市农业农村局", model="1500A2")
    queries = queries_from_lead(lead)
    assert [q.text for q in queries] == ["1500A2", "库车市农业农村局", "1500A2"]
    assert all(q.supplier_name is None for q in queries)


def test_plan_is_empty_without_any_search_key():
    assert queries_from_lead(LeadProfile()) == []
    assert queries_from_lead({}) == []


def test_plan_accepts_a_raw_mapping_and_strips_dirty_values():
    queries = queries_from_lead({"purchaser": " 库车市农业农村局 ", "model": " 1500A2 ",
                                 "category": "生物安全柜", "notice_date": "2025-02-18"})
    assert queries[0].text == "生物安全柜"
    assert queries[0].buyer_name == "库车市农业农村局"
    assert queries[1].text == "1500A2"
    assert queries[0].window_start == "2024-02-19"


def test_generated_queries_are_real_searchquery_objects_with_the_new_fields():
    for query in queries_from_lead(KUCHA_LEAD):
        assert isinstance(query, SearchQuery)
    # 老字段依然要有值，老 provider 靠 text/brand/model 也能跑
    assert queries_from_lead(KUCHA_LEAD)[0].limit == 10


def test_provider_receives_the_new_parameters():
    provider = RecordingProvider()
    service = OfficialSearchService(providers=[provider], fetch_body=False)
    for query in queries_from_lead(KUCHA_LEAD):
        service.run(query)

    received = provider.queries
    assert [q.text for q in received][:6] == ["kcs2025-WT009", "生物安全柜", "1500A2",
                                              "新疆鑫华力科析科技有限公司", "库车市农业农村局",
                                              "1500A2 生物安全柜"]
    assert received[0].project_id == "kcs2025-WT009"
    assert received[1].buyer_name == "库车市农业农村局"
    assert received[3].supplier_name == "新疆鑫华力科析科技有限公司"
    assert all(q.window_start and q.window_end for q in received)


def test_queries_from_candidate_is_untouched():
    """型号选型那条路还在用 `queries_from_candidate`，不能被这一单改动。"""
    from backend.services.official_search import queries_from_candidate

    class Candidate:
        id = "c1"
        brand = "知楚"
        model = "1300A2"

    queries = queries_from_candidate(Candidate(), purpose="tender")
    assert len(queries) == 1
    assert queries[0].text == '"知楚 1300A2" 中标 成交公告 型号 单价'
    assert queries[0].buyer_name is None and queries[0].window_start is None


def test_lead_match_can_outrank_pure_authority():
    """加了「线索吻合度」之后，高权威的无关公告不该再排在有型号信号的来源前面。"""
    high_authority = SearchResult("http://www.ccgp.gov.cn/unrelated.htm", "某单位仪器设备采购中标公告", "")
    lead_hit = SearchResult("http://www.qiye.com/bsc.htm", "库车市农业农村局生物安全柜 知楚 1500A2",
                            "上海知楚 1500A2 生物安全柜")
    provider = RecordingProvider(results={
        "生物安全柜": [high_authority, lead_hit],
    })
    service = OfficialSearchService(providers=[provider], fetch_body=False)

    plain = service.run(SearchQuery(text="生物安全柜", purpose="tender"))
    assert [r.url for r in plain.results] == [high_authority.url, lead_hit.url]

    guided = service.run(SearchQuery(text="生物安全柜", purpose="tender", buyer_name="库车市农业农村局",
                                     model="1500A2", brand="知楚", window_start="2024-02-19",
                                     window_end="2025-02-18"),
                         lead=KUCHA_LEAD)
    assert [r.url for r in guided.results] == [lead_hit.url, high_authority.url]
    assert plain.results != guided.results


def test_run_without_lead_keeps_legacy_ranking_and_no_alignments():
    provider = RecordingProvider(results={
        "生物安全柜": [SearchResult("http://www.ccgp.gov.cn/a.htm", "某单位仪器设备采购中标公告", ""),
                       SearchResult("http://www.qiye.com/b.htm", "生物安全柜", "")],
    })
    service = OfficialSearchService(providers=[provider], fetch_body=False)
    run = service.run(SearchQuery(text="生物安全柜", purpose="tender"))
    assert run.alignments == {}
    assert not any(w.startswith("lead_") for w in run.warnings)
    assert [s.url for s in run.sources] == ["http://www.ccgp.gov.cn/a.htm", "http://www.qiye.com/b.htm"]


def test_run_with_lead_records_the_verdict_for_every_source():
    body = ("湖北大学病毒学和疫苗研究中心动物房建设配套设备货物采购项目成交结果公告\n"
            "供应商名称：优尚和越环境科技有限公司\n"
            "中标（成交）金额： 96.2000000（万元）\n")

    class BodyProvider(RecordingProvider):
        def fetch(self, url, timeout):
            return body

    provider = BodyProvider(results={
        "生物安全柜": [SearchResult(HUBEI_URL, "湖北大学病毒学和疫苗研究中心动物房配套设备成交结果公告", "")],
    })
    service = OfficialSearchService(providers=[provider], fetch_body=True)
    lead = {"buyer": "湖北大学", "supplier": "武汉坎德尼科技有限公司", "amount": "3.25万",
            "model": "1300A2", "brand": "知楚", "category": "生物安全柜", "date": "2024-11-19"}

    plan = queries_from_lead(lead)
    assert plan[0].text == "生物安全柜", "没有 project_id 时第一条就是 buyer_name + 品类词"
    run = service.run(plan[0], lead=lead, attempted_queries=tuple(q.text for q in plan),
                      windows=tuple((q.window_start, q.window_end) for q in plan))

    assert set(run.alignments) == {HUBEI_URL}
    verdict = run.alignments[HUBEI_URL]
    assert verdict.state == "rejected"
    assert "供应商不匹配" in verdict.reason
    assert any(w.startswith("lead_rejected:") for w in run.warnings)
    assert verdict.attempted_queries, "被拒也要留下试过的查询式"
