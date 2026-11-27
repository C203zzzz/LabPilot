"""线索检索编排：请求上限、百度兜底、官方主机筛选、限速停手。

这些用例全部离线（假通道），但它们驱动的是真实公告正文（`tests/fixtures/lead-alignment/`），
所以"闸门判定"这一环和真机跑的是同一段代码。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from backend.domain.search import SearchQuery, SearchResult
from backend.services.lead_alignment import (
    STATE_REJECTED,
    STATE_UNRESOLVED,
    STATE_VERIFIED,
    LeadProfile,
)
from backend.services.lead_search import (
    DEFAULT_REQUEST_BUDGET,
    RequestBudgetExhausted,
    RequestLedger,
    RequestRecord,
    baidu_queries_from_lead,
    filter_official_candidates,
    instrument_provider,
    is_gov_or_edu,
    search_lead,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lead-alignment"

HUBEI_URL = "https://xxgk.hubu.edu.cn/info/1269/12240.htm"
KUCHA_URL = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202502/t20250218_24183413.htm"
HANGJIN_URL = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260921_27374817.htm"

KUCHA_LEAD = LeadProfile(
    buyer="库车市农业农村局", supplier="新疆鑫华力科析科技有限公司", project_id="kcs2025-WT009",
    brand="知楚", model="1500A2", category="生物安全柜", date="2025-02-18",
)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").replace("\r\n", "\n")


class FakeChannel:
    """一个不联网的通道：`_request` 与 `search_once` 的形状和真通道一致。"""

    def __init__(self, name="ccgp", rows=(), bodies=None, error=None, challenge=False):
        self.name = name
        self.rows = list(rows)
        self.bodies = dict(bodies or {})
        self.error = error
        self.challenge = challenge
        self.urls: list[str] = []
        self.search_calls: list[str] = []

    def supports(self, query) -> bool:
        return True

    def search(self, query):
        return self.search_once(query)

    def search_once(self, query):
        self.search_calls.append(query.text)
        self._request(f"http://search.ccgp.gov.cn/bxsearch?kw={quote(query.text)}", 5.0)
        if self.error is not None:
            raise self.error
        if self.challenge:
            from backend.infrastructure.search_providers import ProviderConfigurationError
            raise ProviderConfigurationError("ccgp rate limited (频繁访问)")
        return list(self.rows)

    def _request(self, url, timeout):
        self.urls.append(url)
        return b"x" * 128, "text/html"

    def fetch(self, url, timeout):
        # 真通道的 `fetch` 也走 `_request`（关掉 Jina 之后），所以正文抓取同样计入账本。
        self._request(url, timeout)
        return self.bodies.get(url, "")


def kucha_rows():
    return [SearchResult(KUCHA_URL, "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告", "")]


def kucha_bodies():
    return {KUCHA_URL: fixture_text("kucha-24183413.body.txt")}


def test_ccgp_plan_stops_at_the_request_ceiling_and_says_what_it_did_not_try():
    channel = FakeChannel(rows=[SearchResult("http://www.qiye.com/x.htm", "某单位设备采购公告", "")])
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=channel, include_fallback=False,
                          budget={"ccgp": 2}, timeout=5.0)

    assert outcome.ledger.total <= 2
    assert outcome.budget_exhausted is True
    assert outcome.skipped_queries, "触到上限必须把没跑的查询式列出来，不能静默放弃"
    assert outcome.state == STATE_UNRESOLVED
    assert "请求上限已用尽" in outcome.reason
    assert "未执行" in outcome.reason
    assert "已试查询式" in outcome.reason and "时间窗" in outcome.reason
    # 时间窗必须真的记下来（不是空壳字段）
    assert all(a.window[0] and a.window[1] for a in outcome.attempts)


def test_a_verified_candidate_stops_the_plan_before_it_spends_the_rest():
    channel = FakeChannel(rows=kucha_rows(), bodies=kucha_bodies())
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=channel, include_fallback=False, timeout=5.0)

    assert outcome.state == STATE_VERIFIED
    assert outcome.by_state(STATE_VERIFIED)[0].url == KUCHA_URL
    assert len(channel.search_calls) == 1, "第一条查询就拿到 verified，后面不该再发请求"
    assert outcome.ledger.total == 2, "一次检索 + 一次正文抓取"
    assert outcome.skipped_queries == []


def test_baidu_queries_are_built_from_the_lead_fields_only():
    queries = baidu_queries_from_lead(KUCHA_LEAD)
    assert [q.text for q in queries] == [
        "库车市农业农村局 知楚 生物安全柜 中标",
        "新疆鑫华力科析科技有限公司 知楚 1500A2 中标",
    ]
    assert all(q.purpose == "tender" for q in queries)
    assert all(q.window_start is None and q.window_end is None for q in queries), "百度没有时间窗参数"
    # 弱线索不拼泛搜串：只有一个品类词时返回空
    assert baidu_queries_from_lead(LeadProfile(category="生物安全柜")) == []
    assert baidu_queries_from_lead({}) == []


def test_fallback_candidates_keep_only_official_single_notices():
    rows = [
        SearchResult("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html", "库车市…中标(成交)结果公告", ""),
        SearchResult("https://www.qianlima.com/common/agents_info.jsp?id=564518", "库车市…聚合站转载", ""),
        SearchResult("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/", "库车市采购公告栏目", ""),
        SearchResult("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html", "重复行", ""),
        SearchResult("http://www.ccgp-xinjiang.gov.cn/xwzx/zfcg/1.htm", "新疆政府采购网公告", ""),
    ]
    kept = [r.url for r in filter_official_candidates(rows)]
    assert kept == [
        "https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html",
        "http://www.ccgp-xinjiang.gov.cn/xwzx/zfcg/1.htm",
    ]
    assert is_gov_or_edu("https://a.edu.cn/x.htm") and not is_gov_or_edu("https://a.com/x.htm")


def test_baidu_fallback_runs_only_after_ccgp_fails_to_verify():
    ccgp = FakeChannel(name="ccgp", rows=[SearchResult("http://www.qiye.com/x.htm", "无关公告", "")])
    baidu = FakeChannel(name="baidu", rows=[
        SearchResult("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html",
                     "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告", ""),
        SearchResult("https://www.qianlima.com/i773711056", "聚合站转载", ""),
    ], bodies={
        "https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html": fixture_text("kucha-24183413.body.txt"),
    })
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=ccgp, fallback_provider=baidu, timeout=5.0)

    assert outcome.state == STATE_VERIFIED
    assert [v.url for v in outcome.by_state(STATE_VERIFIED)] == [
        "https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html"]
    assert not any("qianlima" in v.url for v in outcome.verdicts), "聚合站不许进闸门"
    assert [a.channel for a in outcome.attempts].count("baidu") == 1


def test_baidu_candidates_still_have_to_pass_the_gate():
    """百度只是找到候选：正文里型号不符时结论仍是 rejected，不是"百度说对就对"。"""
    ccgp = FakeChannel(name="ccgp", rows=[])
    baidu = FakeChannel(name="baidu", rows=[
        SearchResult("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20260921/i999.html", "杭锦旗…中标（成交）结果公告", ""),
    ], bodies={
        "https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20260921/i999.html": fixture_text("hjj-27374817.body.txt"),
    })
    lead = LeadProfile(buyer="杭锦旗疾病预防控制中心", brand="泰斯特", model="SPX-150BIII",
                       category="生化培养箱", date="2026-09-21")
    outcome = search_lead(lead, ccgp_provider=ccgp, fallback_provider=baidu, timeout=5.0)

    assert outcome.state == STATE_REJECTED
    assert "型号/品牌不匹配" in outcome.by_state(STATE_REJECTED)[0].reason


def test_a_verified_ccgp_result_never_calls_the_fallback():
    ccgp = FakeChannel(name="ccgp", rows=kucha_rows(), bodies=kucha_bodies())
    baidu = FakeChannel(name="baidu", rows=[])
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=ccgp, fallback_provider=baidu, timeout=5.0)

    assert outcome.state == STATE_VERIFIED
    assert baidu.urls == [] and baidu.search_calls == [], "CCGP 已经给出结论，兜底路径不该被打"


def test_a_rate_limit_page_stops_the_whole_lead_including_the_fallback():
    ccgp = FakeChannel(name="ccgp", challenge=True)
    baidu = FakeChannel(name="baidu", rows=[])
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=ccgp, fallback_provider=baidu, timeout=5.0)

    assert outcome.rate_limited is True
    assert baidu.urls == [], "撞限速就停手：兜底也不打"
    assert outcome.state == STATE_UNRESOLVED
    assert "限速页" in outcome.reason
    assert any("rate limited" in w for w in outcome.warnings)


def test_exhausted_fallback_budget_is_reported_not_swallowed():
    ccgp = FakeChannel(name="ccgp", rows=[SearchResult("http://www.qiye.com/x.htm", "无关公告", "")])
    baidu = FakeChannel(name="baidu", rows=[])
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=ccgp, fallback_provider=baidu,
                          budget={"ccgp": 6, "baidu": 0}, timeout=5.0)

    assert outcome.budget_exhausted is True
    for query in baidu_queries_from_lead(KUCHA_LEAD):
        assert f"baidu:{query.text}" in outcome.skipped_queries
    assert all(":" in item for item in outcome.skipped_queries), "未执行的查询式要带通道，别混成一锅"
    assert outcome.ledger.remaining("baidu") == 0


def test_instrument_records_one_row_per_real_request():
    ledger = RequestLedger(allowance={"fake": 5})
    channel = instrument_provider(FakeChannel(name="fake", rows=[SearchResult("https://a.gov.cn/x.htm", "t", "")]), ledger)
    channel.search_once(SearchQuery(text="库车市农业农村局"))

    assert ledger.total == 1
    record = ledger.records[0]
    assert record.channel == "fake" and record.rows == 1
    assert record.payload_bytes == 128 and record.seconds >= 0 and "bxsearch" in record.url


def test_ledger_refuses_the_request_after_the_ceiling():
    ledger = RequestLedger(allowance={"ccgp": 1})
    ledger.ensure_budget("ccgp")
    ledger.add_record(RequestRecord("ccgp", "http://search.ccgp.gov.cn/x", 0.1, 10))
    with pytest.raises(RequestBudgetExhausted):
        ledger.ensure_budget("ccgp")
    assert ledger.remaining("ccgp") == 0
    assert ledger.summary()["channels"]["ccgp"] == {"limit": 1, "spent": 1}


def test_outcome_carries_the_per_request_evidence():
    channel = FakeChannel(rows=kucha_rows(), bodies=kucha_bodies())
    outcome = search_lead(KUCHA_LEAD, ccgp_provider=channel, include_fallback=False, timeout=5.0)
    payload = outcome.to_dict()

    assert payload["ledger"]["total"] == 2
    assert [r["rows"] for r in payload["requests"]] == [1, None] or payload["requests"][0]["rows"] == 1
    assert all(r["seconds"] is not None and r["url"].startswith("http") for r in payload["requests"])
    assert payload["attempts"][0]["window"] == ["2024-02-19", "2025-02-18"]
    assert payload["attempts"][0]["project_id"] == "kcs2025-WT009"


def test_a_body_is_never_fetched_twice_for_the_same_url():
    """同一条线索的相邻两步常常召回同一篇公告（真机实测），正文只该抓一次。"""
    lead = LeadProfile(buyer="库车市农业农村局", category="生物安全柜", date="2025-02-18")
    channel = FakeChannel(rows=kucha_rows(), bodies=kucha_bodies())
    outcome = search_lead(lead, ccgp_provider=channel, include_fallback=False, timeout=5.0)

    fetches = [r for r in outcome.ledger.records if r.url == KUCHA_URL]
    assert len(fetches) == 1, "后面几步又召回了同一篇公告，不该再抓一遍正文"
    assert len(channel.search_calls) == 3, "三步查询本身都要发（关键词/参数不同）"
    assert outcome.ledger.total == 4


def test_index_pages_are_not_treated_as_notices():
    from backend.services.lead_search import is_directory_like

    assert is_directory_like("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/index_4.htm")
    assert is_directory_like("https://www.xjkc.gov.cn/zwgk/csxx/cjgg/")
    assert not is_directory_like("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260921_27374817.htm")


def test_default_budget_covers_search_and_body_fetches():
    assert DEFAULT_REQUEST_BUDGET["ccgp"] >= 4 and DEFAULT_REQUEST_BUDGET["baidu"] >= 2
