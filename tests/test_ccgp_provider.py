"""CCGP site-search provider contract tests (no network)."""
import urllib.parse
from html import escape

import pytest

from backend.domain.search import SearchQuery
from backend.infrastructure.search_providers import CCGPSearchProvider, ProviderConfigurationError


def _keyword(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["kw"][0]


def _results_page(entries):
    items = []
    for url, title, paragraph, stamp, notice_type in entries:
        items.append(f"""
        <li>
            <a href="{url}" style="line-height:18px">{escape(title)}</a>
            <p>{escape(paragraph)}</p>
            <span>{stamp}</span>
            <strong style="font-weight:bolder">{notice_type}</strong>
        </li>""")
    return f"""
    <html><body>
    <ul class="vT-srch-result-list-bid">{''.join(items)}</ul>
    </body></html>"""


ENTRIES = [
    (
        "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260918_27354235.htm",
        "某咨询公司关于某医院生物安全柜等仪器采购项目中标(成交)结果公告",
        "一、项目编号：X1二、项目名称：某医院生物安全柜采购项目三、中标（成交）信息",
        "2026.09.18 15:36:52",
        "中标公告",
    ),
    (
        "http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202609/t20260915_27332769.htm",
        "某大学生物安全柜公开招标公告",
        "项目概况：采购生物安全柜一批",
        "2026.09.15 19:25:24",
        "公开招标",
    ),
]


def make_provider(payload):
    return CCGPSearchProvider(transport=lambda url, timeout: (payload, "text/html; charset=UTF-8"))


def test_parses_result_rows_with_type_and_stamp():
    provider = make_provider(_results_page(ENTRIES).encode("utf-8"))
    results = provider.search_once(SearchQuery("生物安全柜 中标", purpose="tender", limit=10))
    assert [r.provider for r in results] == ["ccgp", "ccgp"]
    first = results[0]
    assert first.url == ENTRIES[0][0]
    assert "中标(成交)结果公告" in first.title
    # Snippet keeps the announcement summary, type, and publication stamp.
    assert "项目名称" in first.snippet and "中标公告" in first.snippet and "2026.09.18" in first.snippet


def test_limit_bounds_parsed_rows():
    provider = make_provider(_results_page(ENTRIES).encode("utf-8"))
    results = provider.search_once(SearchQuery("生物安全柜", purpose="tender", limit=1))
    assert len(results) == 1


def test_rate_limited_page_raises_configuration_error():
    provider = make_provider("<html><body>频繁访问! 您的访问过于频繁,请稍后再试。</body></html>".encode("utf-8"))
    with pytest.raises(ProviderConfigurationError):
        provider.search_once(SearchQuery("生物安全柜", purpose="tender"))


def test_page_without_result_list_returns_empty():
    provider = make_provider("<html><body>共找到 0 条内容</body></html>".encode("utf-8"))
    assert provider.search_once(SearchQuery("生物安全柜", purpose="tender")) == []


@pytest.mark.parametrize("text,purpose,expected", [
    ("生物安全柜 中标 成交 单价 site:gov.cn", "tender", True),
    ('"LA2-4A2" 生物安全柜 采购结果', "price", True),
    ("生物安全柜", "discovery", False),      # not a procurement purpose
    ("centrifuge tender award", "tender", False),  # no Chinese keyword survives
])
def test_supports_requires_procurement_purpose_and_chinese_keywords(text, purpose, expected):
    provider = CCGPSearchProvider()
    assert provider.supports(SearchQuery(text, purpose=purpose)) is expected


def test_engine_query_prefers_the_exact_model_over_the_category():
    """Body search makes the model and the brand searchable; title search never did.

    Measured on the live site: under searchtype=1 (title) the model
    ``TBSPX-150BIII`` and the brand ``泰斯特`` each return 0 rows while
    ``生物安全柜`` returns 178; under searchtype=2 (body) the brand returns 10
    rows whose titles do not contain it at all.
    """
    # Operators and markers still never reach the engine.
    assert CCGPSearchProvider.engine_query(
        '"LA2-4A2" 生物安全柜 中标 成交 单价 site:gov.cn -"赛默飞"') == "生物安全柜"
    assert CCGPSearchProvider.engine_query("生物安全柜") == "生物安全柜"
    # The model leads, because it is the most precise thing to ask for.
    assert CCGPSearchProvider.engine_query(
        '"LA2-4A2" 生物安全柜 中标 成交 单价 site:gov.cn -"赛默飞"',
        brand="赛默飞", model="LA2-4A2") == "LA2-4A2"


def test_query_ladder_walks_from_the_exact_model_outward():
    ladder = CCGPSearchProvider.query_ladder(
        '"TBSPX-150BIII" 生化培养箱 中标 成交 单价', brand="泰斯特", model="TBSPX-150BIII")
    # The full model returned 0 rows live while its alphabetic prefix returned
    # the one notice carrying it, so the prefix follows the exact string.
    #
    # 2026-10-06：「品牌 品类」组合插在单品牌**之前**。原因是 body 索引对带连字符的
    # 型号返回 0 行（实测 ``BJ-1CD`` 与去连字符的 ``BJ1CD`` 都是 0），此时单品牌档
    # 会命中并让 ladder 停下，品类词一次都发不出去——拿回来的永远是"该品牌最新
    # 10 条"（实测「上海博迅」10 行全是药品/食品检验等别的仪器）。组合词实测有效：
    # 「上海博迅 洁净工作台」命中的是含该品类的公告。
    assert ladder == ["TBSPX-150BIII", "TBSPX", "泰斯特 生化培养箱", "泰斯特", "生化培养箱"]


def test_query_ladder_asks_brand_plus_category_before_brand_alone():
    """组合档必须排在单品牌之前，否则品类词永远发不出去（2026-10-06 实测）。"""
    ladder = CCGPSearchProvider.query_ladder(
        '洁净工作台 中标 成交 结果公告 报价明细 单价 -"上海博迅"',
        brand="上海博迅", model="BJ-1CD")
    assert ladder == ["BJ-1CD", "上海博迅 洁净工作台", "上海博迅", "洁净工作台"]
    assert ladder.index("上海博迅 洁净工作台") < ladder.index("上海博迅")


def test_query_ladder_skips_a_prefix_too_short_to_be_specific():
    assert CCGPSearchProvider.query_ladder("DW-86L578S 超低温冰箱 中标",
                                           model="DW-86L578S") == ["DW-86L578S", "超低温冰箱"]


def test_query_ladder_keeps_a_model_that_has_no_category_with_it():
    # This used to be dropped for having no category left, which is exactly
    # what produced "ok, 0 results" for every model-only query.
    assert CCGPSearchProvider.query_ladder("湘仪 XKA-2200 中标 单价",
                                           brand="湘仪", model="XKA-2200") == ["XKA-2200", "XKA", "湘仪"]
    # Only a genuinely unsearchable query produces nothing to send.
    assert CCGPSearchProvider.query_ladder("centrifuge tender award") == []


def test_search_url_uses_body_search_not_title_search():
    provider = CCGPSearchProvider()
    url = provider._search_url("生物安全柜")
    assert "kw=" in url and "%E7%94%9F%E7%89%A9%E5%AE%89%E5%85%A8%E6%9F%9C" in url
    # searchtype=1 matches titles only, which is why a model query used to come
    # back empty; searchtype=2 matches the announcement body.
    assert "searchtype=2" in url
    assert "timeType=6" in url
    assert "start_time=" in url and "end_time=" in url


def test_search_sends_the_model_first_and_stops_at_the_first_hit():
    sent = []

    def transport(url, timeout):
        sent.append(url)
        return _results_page(ENTRIES).encode("utf-8"), "text/html"

    provider = CCGPSearchProvider(transport=transport)
    provider.search_once(SearchQuery('"BSC-1100IIA2" 生物安全柜 中标 成交 单价 site:gov.cn',
                                     purpose="tender", brand="知楚", model="BSC-1100IIA2"))
    assert [_keyword(u) for u in sent] == ["BSC-1100IIA2"]
    assert provider.last_engine_query == "BSC-1100IIA2"


def test_search_falls_back_to_the_prefix_when_the_exact_model_misses():
    sent = []

    def transport(url, timeout):
        sent.append(url)
        # The site's own measured behaviour: the full model matches nothing,
        # its alphabetic prefix matches the notice that carries it.
        payload = _results_page([] if _keyword(url) == "TBSPX-150BIII" else ENTRIES)
        return payload.encode("utf-8"), "text/html"

    provider = CCGPSearchProvider(transport=transport)
    results = provider.search_once(SearchQuery("泰斯特 TBSPX-150BIII 生化培养箱 中标",
                                               purpose="tender", brand="泰斯特",
                                               model="TBSPX-150BIII"))
    assert [r.provider for r in results] == ["ccgp", "ccgp"]
    assert [_keyword(u) for u in sent] == ["TBSPX-150BIII", "TBSPX"]
    # Telemetry must name the keyword really sent, not the preferred one.
    assert provider.last_engine_query == "TBSPX"


def test_fetch_body_uses_provider_decoder():
    body = "<html><body><p>某医院生物安全柜采购项目中标金额：123,000.00元</p></body></html>"
    provider = make_provider(body.encode("utf-8"))
    text = provider.fetch("http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm", 5)
    assert "中标金额" in text and "123,000.00" in text


def test_injected_transport_is_authoritative_even_when_jina_is_enabled(monkeypatch):
    """A fixture transport must not be bypassed by the hosted Jina reader."""
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    monkeypatch.setenv("ENABLE_JINA_READER", "true")
    provider = PublicWebSearchProvider(
        transport=lambda url, timeout: (b"<html><p>fixture body</p></html>", "text/html")
    )
    monkeypatch.setattr(
        "backend.infrastructure.jina_reader.fetch_with_jina",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Jina must not run")),
    )

    assert provider.fetch("https://example.com/fixture", 5) == "fixture body"
