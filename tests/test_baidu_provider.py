"""BaiduSearchProvider contract tests (no network).

The provider exists for one job: take the brand / model / supplier / category
that an aggregator lead carries, send it as one keyword combination, and return
the result list with real URLs so the upper layer can keep the ``.gov.cn`` /
``.edu.cn`` hits. The parse is therefore pinned twice -- once against a verbatim
card from a live SERP, once against a structured page that exercises the
filtering rules.
"""
from html import escape
from pathlib import Path
import urllib.parse

import pytest

from backend.domain.search import SearchQuery
from backend.infrastructure import search_providers as search_providers
from backend.services import research_sources
from backend.infrastructure.search_providers import (
    BaiduSearchProvider, ProviderConfigurationError, PublicWebSearchProvider)


FIXTURES = Path(__file__).parent / "fixtures" / "baidu-serp-2026-09-24"


def query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def card(url, title, snippet="摘要正文", extra=""):
    return f"""
    <div class="result c-container xpath-log new-pmd" srcid="1599" {extra} mu="{url}">
      <div class="cosc-card-content">
        <h3 class="t"><a href="http://www.baidu.com/link?url=WRAPPER">{
            escape(title)}</a></h3>
        <span class="summary-text_15QGa">{escape(snippet)}</span>
      </div>
    </div>"""


def serp(*cards):
    return f"<html><body><div id='content_left'>{''.join(cards)}</div></body></html>".encode("utf-8")


def provider_for(payload: bytes):
    return BaiduSearchProvider(transport=lambda url, timeout: (payload, "text/html"))


# ---------------------------------------------------------------------------
# Parsing a real page
# ---------------------------------------------------------------------------

def test_a_verbatim_card_from_the_live_serp_yields_its_real_url():
    payload = (FIXTURES / "organic-card.html").read_bytes()
    rows = BaiduSearchProvider._parse_baidu_results(payload, 10)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.xjkc.gov.cn/zwgk/csxx/cjgg/20250220/i1145452.html"
    assert rows[0]["title"].startswith("库车市现代农作物种子科研及加工基地建设项目设备采购中标")
    # The snippet comes from the card's own summary, not from its title.
    assert "库车市农业农村局" in rows[0]["snippet"]
    assert rows[0]["snippet"] != rows[0]["title"]


def test_the_redirect_wrapper_is_not_returned_as_the_result_url():
    """The wrapper costs a second request; the card's own mu already resolved it."""
    payload = (FIXTURES / "organic-card.html").read_bytes()
    row = BaiduSearchProvider._parse_baidu_results(payload, 10)[0]
    assert "baidu.com/link" not in row["url"]
    assert row["url"].startswith("https://")


# ---------------------------------------------------------------------------
# Parsing rules
# ---------------------------------------------------------------------------

def test_baidu_owned_cards_and_url_less_cards_are_skipped():
    payload = serp(
        card("https://top.baidu.com/board", "百度热搜"),
        card("http://nourl.ubs.baidu.com/279", "广告位"),
        card("/link?url=RELATIVE", "相对链接卡片"),
        '<div class="result-op c-container new-pmd" mu="http://x.com.cn/y"><h3>无 result 类</h3></div>',
        card("https://www.xjkc.gov.cn/a/1.html", "库车市某某采购项目中标(成交)结果公告"),
    )
    rows = BaiduSearchProvider._parse_baidu_results(payload, 10)
    assert [r["url"] for r in rows] == ["https://www.xjkc.gov.cn/a/1.html"]


def test_duplicate_urls_collapse_to_one_row():
    same = card("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm", "某公告中标结果")
    rows = BaiduSearchProvider._parse_baidu_results(serp(same, same), 10)
    assert len(rows) == 1


def test_a_card_without_a_title_is_not_a_lead():
    payload = serp(f'<div class="result c-container" mu="http://a.gov.cn/x"><h3></h3></div>')
    assert BaiduSearchProvider._parse_baidu_results(payload, 10) == []


def test_the_limit_bounds_the_parsed_rows():
    payload = serp(*[card(f"http://www.ccgp.gov.cn/cggg/{i}.htm", f"公告 {i} 中标")
                     for i in range(5)])
    assert len(BaiduSearchProvider._parse_baidu_results(payload, 2)) == 2


def test_a_page_with_no_results_is_empty_not_an_error():
    assert BaiduSearchProvider._parse_baidu_results(
        "<html><body>没有找到相关结果</body></html>".encode("utf-8"), 10) == []


@pytest.mark.parametrize("markup", [
    "<html><title>百度安全验证</title><body>请输入验证码</body></html>",
    "<html><body>请稍后重试 <a href='https://wappass.baidu.com/passport'>wappass</a></body></html>",
    "<html><title>访问验证</title><body>x</body></html>",
])
def test_a_verification_page_is_a_provider_error_not_an_empty_search(markup):
    with pytest.raises(ProviderConfigurationError, match="access challenge"):
        BaiduSearchProvider._parse_baidu_results(markup.encode("utf-8"), 10)


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------

def test_search_once_sends_the_keyword_combination_to_the_serp():
    sent = []
    provider = BaiduSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (serp(card("http://www.ccgp.gov.cn/cggg/t1.htm", "知楚生物安全柜中标公告",
                                       "采购人：库车市农业农村局")), "text/html")))
    results = provider.search_once(SearchQuery("知楚 1300A2 生物安全柜 中标", purpose="tender"))
    assert len(sent) == 1
    parts = urllib.parse.urlsplit(sent[0])
    assert parts.scheme == "https" and parts.netloc == "www.baidu.com" and parts.path == "/s"
    assert query(sent[0])["wd"] == ["知楚 1300A2 生物安全柜 中标"]
    assert query(sent[0])["rn"] == ["20"]
    # Chinese travels percent-encoded, never raw.
    assert "知楚" not in sent[0]
    assert urllib.parse.quote_plus("知楚 1300A2 生物安全柜 中标") in sent[0]
    assert [r.provider for r in results] == ["baidu"]
    assert results[0].url == "http://www.ccgp.gov.cn/cggg/t1.htm"


def test_engine_query_keeps_an_exclusion_because_this_engine_honors_it():
    assert BaiduSearchProvider.engine_query('培养箱  技术参数 -"原品牌"') == '培养箱 技术参数 -"原品牌"'


def test_the_budget_flag_stays_false_because_no_wrapper_fetch_is_spent():
    assert BaiduSearchProvider.resolves_result_links is False


def test_search_merges_the_tender_expansions_and_keeps_only_plausible_notices():
    sent = []
    payload = serp(
        card("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm", "知楚生物安全柜中标结果公告",
             "采购人：库车市农业农村局 中标金额 12,000 元"),
        card("https://shop.example.com/item/1300A2", "ZSC-1300A2 生物安全柜 报价", "厂商页面"),
    )
    provider = BaiduSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (payload, "text/html")))
    results = provider.search(SearchQuery("知楚 1300A2 生物安全柜 中标", purpose="tender", limit=10))
    # One page for the query plus the base class's two tender expansions.
    assert len(sent) == 3
    assert [r.url for r in results] == ["http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm"]


def test_search_deduplicates_across_keyword_combinations():
    sent = []
    payload = serp(card("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm", "知楚中标公告"))
    provider = BaiduSearchProvider(transport=lambda url, timeout: (
        sent.append(url) or (payload, "text/html")))
    results = provider.search(SearchQuery("知楚 中标", purpose="tender", limit=10))
    assert len(sent) == 3
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Request headers (设计规范 recipe P3)
# ---------------------------------------------------------------------------

#: The shared profile's own Accept, which Bing/body reads keep.
BASE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
#: The browser navigation value the live SERP was measured with.
MEASURED_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8")


class _FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self._payload = payload
        self.headers = {"Content-Type": "text/html"}

    def read(self, *args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_on_the_wire(monkeypatch, payload: bytes) -> dict:
    """Record the headers of the request the provider actually builds.

    An injected transport answers *before* the header dict is built, so it
    cannot see them. This stubs the opener one level lower instead -- the last
    point before a real request would leave the process -- so nothing here
    touches the network and the assertion is about the wire, not about a getter.
    """
    seen: dict = {}

    class _Opener:
        def open(self, request, timeout=None):
            seen.update(request.headers)
            return _FakeResponse(payload)

    monkeypatch.setattr(search_providers.urllib.request, "build_opener",
                        lambda *a, **k: _Opener())
    monkeypatch.setattr(search_providers, "impersonation_available", lambda: False)
    monkeypatch.setattr(research_sources, "network_url", lambda url: url)
    return seen


def _lowered(headers) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


def test_the_three_measured_headers_reach_the_wire_for_baidu(monkeypatch):
    payload = serp(card("http://www.ccgp.gov.cn/cggg/t1.htm", "知楚生物安全柜中标公告",
                        "采购人：库车市农业农村局"))
    seen = _capture_on_the_wire(monkeypatch, payload)
    results = BaiduSearchProvider().search_once(
        SearchQuery("知楚 1300A2 生物安全柜 中标", purpose="tender"))
    sent = _lowered(seen)   # after the request: the capture fills during it
    assert [r.url for r in results] == ["http://www.ccgp.gov.cn/cggg/t1.htm"]
    assert sent["referer"] == "https://www.baidu.com/"
    assert sent["upgrade-insecure-requests"] == "1"
    assert sent["accept"] == MEASURED_ACCEPT
    # The rest of the profile is the base one, unchanged.
    assert sent["accept-language"] == "zh-CN,zh;q=0.9,en;q=0.8"
    assert sent["user-agent"].endswith("Chrome/124.0.0.0 Safari/537.36")


def test_only_baidu_overrides_the_shared_profile():
    url = "https://www.baidu.com/s?wd=x"
    base = PublicWebSearchProvider()._request_headers(url)
    assert BaiduSearchProvider()._request_headers(url) != base


def test_the_shared_profile_bing_uses_is_byte_for_byte_unchanged(monkeypatch):
    """Bing goes through the same ``_request``; it must keep the old headers."""
    seen = _capture_on_the_wire(monkeypatch, b"<html><body>ok</body></html>")
    PublicWebSearchProvider()._request("https://www.bing.com/search?q=x", 5.0)
    sent = _lowered(seen)   # after the request: the capture fills during it
    assert sent["accept"] == BASE_ACCEPT
    assert sent["accept-language"] == "zh-CN,zh;q=0.9,en;q=0.8"
    assert "referer" not in sent
    assert "upgrade-insecure-requests" not in sent
