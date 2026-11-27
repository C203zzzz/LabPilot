"""Structured CCGP query parameters and the <=366-day window walk (no network).

Two contracts are pinned here:

* ``CCGPSearchProvider._search_url`` gained ``buyer_name`` / ``project_id`` /
  ``agent_name`` / ``display_zone`` and an explicit window. Called the way the
  existing call sites call it, it must produce the byte-identical URL it
  produced before those parameters existed -- 72 ``SearchQuery(`` call sites
  depend on that (``设计规范`` §3.1).
* A window wider than ``CCGP_TRUTHFUL_WINDOW_DAYS`` is asked for in pieces and
  merged by URL, because the site silently returns *fewer* rows for a wider box
  instead of erroring (``search_providers.py`` CCGP_TRUTHFUL_WINDOW_DAYS).
"""
from datetime import datetime, timedelta
from html import escape
import time
import urllib.parse

import pytest

from backend.domain.search import SearchQuery
from backend.infrastructure import search_providers
from backend.infrastructure.search_providers import (
    CCGP_MAX_WINDOW_DAYS, CCGP_TRUTHFUL_WINDOW_DAYS, CCGPSearchProvider,
    ProviderConfigurationError, ccgp_window_segments,
)


def query(url: str, keep_blank: bool = False) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=keep_blank)


def page(entries):
    items = "".join(
        f'<li><a href="{url}">{escape(title)}</a><p>{escape(title)} 公告正文</p>'
        f'<span>2026.09.18 15:36:52</span><strong>中标公告</strong></li>'
        for url, title in entries)
    return f'<html><body><ul class="vT-srch-result-list-bid">{items}</ul></body></html>'.encode("utf-8")


ROWS = [
    ("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260918_1.htm", "某单位生物安全柜采购项目中标公告"),
    ("http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/t20260915_2.htm", "某大学生物安全柜公开招标公告"),
]


# ---------------------------------------------------------------------------
# URL assembly
# ---------------------------------------------------------------------------

def test_the_default_url_is_byte_identical_to_the_pre_change_builder(monkeypatch):
    """The compatibility contract: no new parameter, no URL change at all."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 24, 12, 0, 0)

    monkeypatch.setattr(search_providers, "datetime", Frozen)
    end = Frozen.now()
    start = end - timedelta(days=365)
    legacy = "http://search.ccgp.gov.cn/bxsearch?" + urllib.parse.urlencode({
        "searchtype": "2", "page_index": "1", "bidSort": "0",
        "buyerName": "", "projectId": "", "pinMu": "0", "bidType": "0",
        "dbselect": "bidx", "kw": "生物安全柜", "timeType": "6",
        "start_time": start.strftime("%Y:%m:%d"), "end_time": end.strftime("%Y:%m:%d"),
    })
    assert CCGPSearchProvider()._search_url("生物安全柜") == legacy


def test_the_default_url_carries_no_new_parameter_name():
    url = CCGPSearchProvider()._search_url("生物安全柜")
    assert "agentName" not in url and "displayZone" not in url
    # The two filters the site form always shows travel as empty, exactly as before.
    assert "buyerName=&" in url and "projectId=&" in url


@pytest.mark.parametrize("kwargs,param,value", [
    ({"buyer_name": "库车市农业农村局"}, "buyerName", "库车市农业农村局"),
    ({"project_id": "2921-20250429-667"}, "projectId", "2921-20250429-667"),
    ({"agent_name": "新疆经开项目管理有限公司"}, "agentName", "新疆经开项目管理有限公司"),
    ({"display_zone": "湖北"}, "displayZone", "湖北"),
])
def test_each_structured_parameter_reaches_the_query_string(kwargs, param, value):
    url = CCGPSearchProvider()._search_url("知楚", **kwargs)
    # Round-trips through percent-encoding: the site must read back the exact
    # value, including Chinese.
    assert query(url)[param] == [value]
    assert urllib.parse.quote_plus(value) in url


@pytest.mark.parametrize("value", ["库车市农业农村局", "新疆经开项目管理有限公司", "知楚"])
def test_chinese_parameter_values_are_percent_encoded_not_echoed(value):
    url = CCGPSearchProvider()._search_url(value, buyer_name=value)
    assert value not in url
    assert "buyerName=" + urllib.parse.quote_plus(value) in url
    assert "kw=" + urllib.parse.quote_plus(value) in url


def test_the_four_parameters_travel_together_without_disturbing_the_rest():
    url = CCGPSearchProvider()._search_url(
        "知楚", page=3, buyer_name="库车市农业农村局", project_id="2921-20250429-667",
        agent_name="某代理", display_zone="新疆")
    params = query(url)
    assert params["searchtype"] == ["2"] and params["page_index"] == ["3"]
    assert params["kw"] == ["知楚"]
    assert params["buyerName"] == ["库车市农业农村局"]
    assert params["projectId"] == ["2921-20250429-667"]
    assert params["agentName"] == ["某代理"]
    assert params["displayZone"] == ["新疆"]


def test_an_explicit_window_replaces_the_default_one():
    url = CCGPSearchProvider()._search_url("知楚", start="2024-01-01", end="2024-12-31")
    assert query(url)["start_time"] == ["2024:01:01"]
    assert query(url)["end_time"] == ["2024:12:31"]
    assert query(url)["timeType"] == ["6"]


def test_a_bad_window_bound_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="zero-padded ISO date"):
        CCGPSearchProvider()._search_url("知楚", start="2024/01/01", end="2024-12-31")


# ---------------------------------------------------------------------------
# Window segmentation (pure)
# ---------------------------------------------------------------------------

def spans(segments):
    return [(datetime.strptime(s, "%Y-%m-%d").date(), datetime.strptime(e, "%Y-%m-%d").date())
            for s, e in segments]


def inclusive_days(segments):
    return sum((end - start).days + 1 for start, end in spans(segments))


def test_one_year_of_leap_days_is_exactly_one_segment():
    # 2024-01-01..2024-12-31 is 366 days inclusive: the widest faithful box.
    assert ccgp_window_segments("2024-01-01", "2024-12-31") == [("2024-01-01", "2024-12-31")]


def test_367_inclusive_days_split_into_two_contiguous_segments():
    segments = ccgp_window_segments("2023-01-01", "2024-01-02")
    assert segments == [("2023-01-01", "2024-01-01"), ("2024-01-02", "2024-01-02")]
    assert inclusive_days(segments) == 367


def test_a_window_inside_one_year_stays_one_segment():
    assert ccgp_window_segments("2024-03-01", "2024-06-30") == [("2024-03-01", "2024-06-30")]


@pytest.mark.parametrize("start,end", [
    ("2024-06-01", "2025-06-30"),
    ("2023-01-01", "2025-12-31"),
    ("2022-02-28", "2026-09-24"),
    ("2020-01-01", "2020-12-31"),
    ("2024-12-31", "2024-12-31"),
    ("2026-09-24", "2026-09-24"),
])
def test_segments_are_contiguous_inclusive_and_within_the_site_limit(start, end):
    segments = ccgp_window_segments(start, end)
    first, last = (datetime.strptime(start, "%Y-%m-%d").date(),
                   datetime.strptime(end, "%Y-%m-%d").date())
    capped_start = max(first, last - timedelta(days=CCGP_MAX_WINDOW_DAYS - 1))
    assert segments, "a window always produces at least one request"
    assert segments[0][0] == capped_start.strftime("%Y-%m-%d") and segments[-1][1] == end
    for one_start, one_end in spans(segments):
        assert (one_end - one_start).days + 1 <= CCGP_TRUTHFUL_WINDOW_DAYS
    for (_, previous_end), (next_start, _) in zip(segments, segments[1:]):
        assert (datetime.strptime(next_start, "%Y-%m-%d")
                - datetime.strptime(previous_end, "%Y-%m-%d")).days == 1
    assert inclusive_days(segments) == min((last - first).days + 1, CCGP_MAX_WINDOW_DAYS)


def test_a_window_over_three_years_is_truncated_to_the_most_recent_three():
    segments = ccgp_window_segments("2020-01-01", "2026-09-24")
    assert len(segments) == 3
    assert segments[-1][1] == "2026-09-24"
    assert inclusive_days(segments) == CCGP_MAX_WINDOW_DAYS
    expected_start = (datetime.strptime("2026-09-24", "%Y-%m-%d")
                      - timedelta(days=CCGP_MAX_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    assert segments[0][0] == expected_start


def test_a_missing_end_means_today_and_a_missing_start_means_the_walk_limit():
    today = datetime(2026, 9, 24)
    assert ccgp_window_segments("2026-09-01", now=today) == [("2026-09-01", "2026-09-24")]
    open_ended = ccgp_window_segments(None, "2026-09-24", now=today)
    assert len(open_ended) <= 3 and open_ended[-1][1] == "2026-09-24"
    assert inclusive_days(open_ended) == CCGP_MAX_WINDOW_DAYS
    both = ccgp_window_segments(None, None, now=today)
    assert both[-1][1] == "2026-09-24" and inclusive_days(both) == CCGP_MAX_WINDOW_DAYS
    assert sum(1 for _ in both) <= 3


def test_blank_bounds_mean_missing_bounds():
    blank = ccgp_window_segments("   ", " 2026-09-24 ", now=datetime(2026, 9, 24))
    assert blank == ccgp_window_segments(None, "2026-09-24", now=datetime(2026, 9, 24))
    assert blank[-1][1] == "2026-09-24"


@pytest.mark.parametrize("start,end,message", [
    ("2024-12-31", "2024-01-01", "must not be after"),
    ("2024-1-1", "2024-12-31", "zero-padded"),
    ("2024/01/01", "2024-12-31", "zero-padded"),
    ("20240101", "2024-12-31", "zero-padded"),
    ("2024-13-01", "2024-12-31", "zero-padded"),
    ("2024-02-30", "2024-12-31", "zero-padded"),
    ("2024-01-01", "2024-2-30", "zero-padded"),
])
def test_bad_bounds_and_inverted_windows_raise(start, end, message):
    with pytest.raises(ValueError, match=message):
        ccgp_window_segments(start, end)


@pytest.mark.parametrize("kwargs", [{"window_days": 0}, {"window_days": -5}, {"max_days": 0}])
def test_non_positive_loop_bounds_are_refused(kwargs):
    """A zero-width segment would turn one query into thousands of requests."""
    with pytest.raises(ValueError, match="must be positive"):
        ccgp_window_segments("2020-01-01", "2024-01-01", **kwargs)


def test_one_window_bound_alone_is_refused_instead_of_completed_from_now():
    with pytest.raises(ValueError, match="given together"):
        CCGPSearchProvider()._search_url("知楚", start="2024-01-01")
    with pytest.raises(ValueError, match="given together"):
        CCGPSearchProvider()._search_url("知楚", end="2024-12-31")


# ---------------------------------------------------------------------------
# The provider walk: requests, merging, and stopping
# ---------------------------------------------------------------------------

def test_a_project_id_only_query_is_sent_even_though_no_keyword_survives():
    """The exact filter is the query: a project id is not a Chinese category word."""
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    query_obj = SearchQuery("2921-20250429-667", purpose="tender",
                            project_id="2921-20250429-667",
                            window_start="2025-01-01", window_end="2025-12-31")
    results = provider.search_once(query_obj)
    assert len(sent) == 1
    assert query(sent[0])["projectId"] == ["2921-20250429-667"]
    # Measured live: an empty keyword with the exact filter returned the
    # project's 2 announcements, keyword=project id returned only 1. So the
    # leftover text must NOT be ANDed onto the filter.
    assert query(sent[0], keep_blank=True)["kw"] == [""]
    assert len(results) == 2


def test_a_project_id_carried_inside_a_normal_query_still_uses_the_ladder():
    """The exact filter does not overload a query that already has keywords."""
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    provider.search_once(SearchQuery("生物安全柜 中标", purpose="tender",
                                     project_id="2921-20250429-667"))
    assert [query(u)["kw"][0] for u in sent] == ["生物安全柜"]
    assert query(sent[0])["projectId"] == ["2921-20250429-667"]


def test_a_plain_unsearchable_query_without_filters_is_still_never_sent():
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    assert provider.search_once(SearchQuery("site:gov.cn filetype:pdf 中标 成交",
                                            purpose="tender")) == []
    assert sent == []


def test_supports_accepts_the_filter_only_query_and_still_refuses_the_empty_one():
    provider = CCGPSearchProvider()
    assert provider.supports(SearchQuery("2921-20250429-667", purpose="tender",
                                         project_id="2921-20250429-667"))
    assert provider.supports(SearchQuery("库车市农业农村局 采购", purpose="tender",
                                         buyer_name="库车市农业农村局"))
    assert not provider.supports(SearchQuery("site:gov.cn filetype:pdf 中标 成交",
                                             purpose="tender"))


def test_a_filter_only_query_leaves_the_keyword_empty():
    """No keyword candidate survives, so the exact filter does the work alone."""
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    provider.search_once(SearchQuery("BSC-1100IIA2", purpose="tender",
                                     buyer_name="库车市农业农村局"))
    assert len(sent) == 1
    assert query(sent[0])["buyerName"] == ["库车市农业农村局"]
    assert query(sent[0], keep_blank=True)["kw"] == [""]


def test_a_chinese_buyer_name_left_in_the_text_travels_as_the_keyword_too():
    """A keyword that does survive the ladder is still sent: buyerName AND kw."""
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    provider.search_once(SearchQuery("库车市农业农村局", purpose="tender",
                                     buyer_name="库车市农业农村局"))
    assert len(sent) == 1
    assert query(sent[0])["buyerName"] == ["库车市农业农村局"]
    assert query(sent[0])["kw"] == ["库车市农业农村局"]


def test_without_a_window_every_ladder_candidate_gets_one_request():
    sent = []
    empty = "<html><body>共找到 0 条内容</body></html>".encode("utf-8")
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (empty, "text/html")))
    provider.search_once(SearchQuery("泰斯特 TBSPX-150BIII 生化培养箱 中标", purpose="tender",
                                     brand="泰斯特", model="TBSPX-150BIII"))
    # 2026-10-06：「品牌 品类」组合档插在单品牌之前，候选因此多一个
    # （理由见 test_ccgp_provider.py::test_query_ladder_asks_brand_plus_category_before_brand_alone）。
    assert [query(u)["kw"][0] for u in sent] == [
        "TBSPX-150BIII", "TBSPX", "泰斯特 生化培养箱", "泰斯特", "生化培养箱"]


def test_a_window_inside_the_limit_is_one_request_and_carries_the_window():
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (b"<html><body>0</body></html>", "text/html")))
    provider.search_once(SearchQuery("知楚", purpose="tender",
                                     window_start="2024-01-01", window_end="2024-12-31"))
    assert len(sent) == 1
    assert query(sent[0])["start_time"] == ["2024:01:01"]
    assert query(sent[0])["end_time"] == ["2024:12:31"]


def test_structured_filters_really_reach_the_sent_url():
    """The reverse check for "the parameter is accepted but never used"."""
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (b"<html><body>0</body></html>", "text/html")))
    provider.search_once(SearchQuery("知楚", purpose="tender", buyer_name="库车市农业农村局",
                                     project_id="2921-20250429-667",
                                     window_start="2024-01-01", window_end="2024-12-31"))
    params = query(sent[0])
    assert params["buyerName"] == ["库车市农业农村局"]
    assert params["projectId"] == ["2921-20250429-667"]


def test_provider_level_filters_reach_every_request():
    sent = []
    provider = CCGPSearchProvider(agent_name="某代理", display_zone="新疆",
                                 transport=lambda url, timeout: (
                                     sent.append(url) or (page([]), "text/html")))
    provider.search_once(SearchQuery("知楚", purpose="tender",
                                     window_start="2023-01-01", window_end="2025-12-31"))
    assert len(sent) == 3
    assert all(query(u)["agentName"] == ["某代理"] for u in sent)
    assert all(query(u)["displayZone"] == ["新疆"] for u in sent)


def test_a_three_year_window_is_three_requests_merged_by_url_without_duplicates():
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    results = provider.search_once(SearchQuery("生物安全柜", purpose="tender",
                                               window_start="2023-09-25", window_end="2026-09-24"))
    assert len(sent) == 3
    assert [query(u)["start_time"][0] for u in sent] == ["2023:09:25", "2024:09:25", "2025:09:26"]
    assert [query(u)["end_time"][0] for u in sent] == ["2024:09:24", "2025:09:25", "2026:09:24"]
    # Same two announcements in all three segments: the merge keeps each once.
    assert [r.url for r in results] == [row[0] for row in ROWS]
    assert len(results) == 2


def test_segments_with_distinct_rows_are_all_kept():
    def transport(url, timeout):
        start = query(url)["start_time"][0].replace(":", "-")
        return page([(f"http://www.ccgp.gov.cn/cggg/dfgg/zbgg/{start}_1.htm",
                      f"{start} 生物安全柜中标公告")]), "text/html"

    provider = CCGPSearchProvider(transport=transport)
    results = provider.search_once(SearchQuery("生物安全柜", purpose="tender", limit=10,
                                               window_start="2023-09-25", window_end="2026-09-24"))
    assert len(results) == 3
    assert len({r.url for r in results}) == 3


def test_the_ladder_still_stops_at_its_first_hit_inside_each_segment():
    sent = []

    def transport(url, timeout):
        sent.append(url)
        keyword = query(url)["kw"][0]
        return (page(ROWS) if keyword == "知楚" else page([])), "text/html"

    provider = CCGPSearchProvider(transport=transport)
    results = provider.search_once(SearchQuery("知楚 生物安全柜 中标", purpose="tender",
                                               brand="知楚", model="BSC-1100IIA2",
                                               window_start="2023-09-25", window_end="2026-09-24"))
    # 组合档「知楚 生物安全柜」在这个替身上返回 0 行（它只认「知楚」），
    # 于是每段多一次请求，命中仍停在「知楚」——"第一命中即停"这条行为没变。
    assert [query(u)["kw"][0] for u in sent] == [
        "BSC-1100IIA2", "BSC", "知楚 生物安全柜", "知楚"] * 3
    assert provider.last_engine_query == "知楚"
    assert [r.url for r in results] == [row[0] for row in ROWS]


def test_the_merge_stops_once_the_caller_has_enough_rows():
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page(ROWS), "text/html")))
    results = provider.search_once(SearchQuery("生物安全柜", purpose="tender", limit=1,
                                               window_start="2023-09-25", window_end="2026-09-24"))
    assert len(sent) == 1 and len(results) == 1


def test_a_rate_limited_segment_stops_the_round_and_arms_the_pause():
    sent = []

    def transport(url, timeout):
        sent.append(url)
        if len(sent) == 2:
            return "<html><title>频繁访问!中国政府采购网</title></html>".encode("utf-8"), "text/html"
        return page([]), "text/html"

    provider = CCGPSearchProvider(transport=transport)
    before = time.monotonic()
    with pytest.raises(ProviderConfigurationError, match="rate limited"):
        provider.search_once(SearchQuery("生物安全柜", purpose="tender",
                                         window_start="2023-09-25", window_end="2026-09-24"))
    # The third segment is never attempted, and the channel is paused.
    assert len(sent) == 2
    assert provider.paused_until >= before + provider.rate_limit_cooldown - 1


def test_the_walk_never_exceeds_three_requests_even_for_a_decade():
    sent = []
    provider = CCGPSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (page([]), "text/html")))
    provider.search_once(SearchQuery("生物安全柜", purpose="tender",
                                     window_start="2010-01-01", window_end="2026-09-24"))
    assert len(sent) == 3
