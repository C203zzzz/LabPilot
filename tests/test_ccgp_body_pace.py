"""Body-read pacing and bounded parallel fetching (offline).

The two clocks this file pins down:

* the search subdomain keeps the original 6s module-level floor;
* announcement bodies get their own floor and a hard ceiling on how many may
  be in flight at once.

Every test here is offline. The pacing lines are exercised by replacing
``PublicWebSearchProvider._request`` (the base implementation the CCGP adapter
calls through ``super()``), which is the same seam an injected transport would
use -- but a transport short-circuits the pacing code, so it cannot test it.
"""
import threading
import time

import pytest

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.search_providers import (
    CCGP_BODY_FETCH_WORKERS,
    CCGP_BODY_MIN_INTERVAL,
    CCGPSearchProvider,
    ProviderConfigurationError,
    PublicWebSearchProvider,
)
from backend.services.official_search import OfficialSearchService

SEARCH_URL = "http://search.ccgp.gov.cn/bxsearch?kw=%E7%94%9F%E7%89%A9%E5%AE%89%E5%85%A8%E6%9F%9C"
BODY_URL = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260918_27354235.htm"

RATE_LIMIT_PAGE = "<html><head><title>频繁访问!中国政府采购网</title></head><body>您的访问过于频繁</body></html>".encode("utf-8")
DEFAULT_BODY = "<html><body>公告正文 中标 成交 单价</body></html>".encode("utf-8")


class Recorder:
    """Stands in for the base HTTP request; records start times, returns bytes."""

    def __init__(self, payload=DEFAULT_BODY):
        self.payload = payload
        self.starts: list[float] = []
        self.urls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, url, timeout):
        with self.lock:
            self.starts.append(time.monotonic())
            self.urls.append(url)
        return self.payload, "text/html"


@pytest.fixture()
def base_request(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(PublicWebSearchProvider, "_request", recorder)
    return recorder


def _gap(recorder) -> float:
    assert len(recorder.starts) == 2, recorder.urls
    return recorder.starts[1] - recorder.starts[0]


def test_search_subdomain_keeps_its_own_floor(base_request):
    """The list-search line is unchanged: the 6s global still paces it."""
    provider = CCGPSearchProvider(min_interval=0.15)
    provider._request(SEARCH_URL, 5.0)
    provider._request(SEARCH_URL, 5.0)
    assert _gap(base_request) >= 0.12


def test_body_read_is_not_delayed_by_the_search_clock(base_request):
    """A body read right after a list search must not inherit its floor.

    This is the regression the whole change exists for: with one shared clock a
    body read paid ``min_interval`` even though the search request had just
    been sent. The body clock is a different module-level global, so the search
    floor is not visible to it.
    """
    provider = CCGPSearchProvider(min_interval=5.0, body_min_interval=0.0)
    provider._request(SEARCH_URL, 5.0)
    started = time.monotonic()
    provider._request(BODY_URL, 5.0)
    assert time.monotonic() - started < 1.0, "body read waited on the search clock"
    assert base_request.urls == [SEARCH_URL, BODY_URL]


def test_body_line_has_its_own_floor(base_request):
    provider = CCGPSearchProvider(body_min_interval=0.15)
    provider._request(BODY_URL, 5.0)
    provider._request(BODY_URL, 5.0)
    assert _gap(base_request) >= 0.12


def test_body_line_floor_is_the_measured_value():
    """The shipped floor is the measured-safe value, not an invented one."""
    assert CCGP_BODY_MIN_INTERVAL == 1.0
    provider = CCGPSearchProvider()
    assert provider.body_min_interval == CCGP_BODY_MIN_INTERVAL
    assert provider.min_interval == 6.0, "the search floor must not be loosened"


def test_body_rate_limit_page_stops_the_round(monkeypatch):
    """A rate-limit page on a body read is a stop signal, not an article.

    2026-10-05 起多一次机会：第一次撞上限流页按站点节奏退避后重发一次
    （"不要一失败就放弃查询"），重发仍撞上才设满冷却——此后这个站不再收到任何请求。
    """
    recorder = Recorder(RATE_LIMIT_PAGE)
    monkeypatch.setattr(PublicWebSearchProvider, "_request", recorder)
    provider = CCGPSearchProvider(rate_limit_cooldown=60.0, transient_backoff=0.0)

    with pytest.raises(ProviderConfigurationError):
        provider._request(BODY_URL, 5.0)
    assert provider.paused_until > time.monotonic()
    assert len(recorder.starts) == 2, "退避重发一次之后仍限流，才算这一轮失败"

    # Armed cooldown: the next call is refused without touching the network.
    with pytest.raises(ProviderConfigurationError):
        provider._request(BODY_URL, 5.0)
    assert len(recorder.starts) == 2, "a paused provider must not send another request"


def test_rate_limit_screening_ignores_large_payloads():
    """A big attachment cannot be the ~3 KB rate-limit page; it is not scanned."""
    from backend.infrastructure.search_providers import _ccgp_looks_rate_limited

    assert _ccgp_looks_rate_limited(RATE_LIMIT_PAGE)
    assert not _ccgp_looks_rate_limited(b"%PDF-1.4" + b"\x00" * 300_000 + "频繁访问".encode())
    assert not _ccgp_looks_rate_limited(b"<html>ordinary announcement</html>")


class FakeFetchProvider:
    """Records concurrency, optionally fails one URL, never touches the network."""

    name = "fake"

    def __init__(self, *, fail_urls=(), body="公告正文 中标 成交 单价", delay=0.05):
        self.fail_urls = set(fail_urls)
        self.body = body
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls: list[str] = []

    def supports(self, query):
        return True

    def search(self, query):
        return []

    def fetch(self, url, timeout):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.calls.append(url)
        try:
            time.sleep(self.delay)
            if url in self.fail_urls:
                raise IOError(f"boom {url}")
            return self.body
        finally:
            with self.lock:
                self.in_flight -= 1


def _results(count):
    return [SearchResult(f"http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260918_273542{index:02d}.htm",
                         "某医院生物安全柜采购项目中标(成交)结果公告", "", "fake")
            for index in range(count)]


def _run(service, results, **kwargs):
    query = SearchQuery(text="生物安全柜 中标", limit=len(results), purpose="tender")
    return service.run(query, results=results, **kwargs)


def test_body_fetches_are_parallel_but_capped():
    provider = FakeFetchProvider()
    service = OfficialSearchService(providers=[provider], fetch_body=True, timeout=5.0)
    run = _run(service, _results(8))

    assert len(provider.calls) == 8
    assert provider.peak > 1, "bodies were fetched one at a time"
    assert provider.peak <= CCGP_BODY_FETCH_WORKERS, provider.peak
    assert all(source.body_status.value == "fetched" for source in run.sources)


def test_one_dead_body_does_not_take_the_round_down():
    dead = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260918_27354203.htm"
    provider = FakeFetchProvider(fail_urls={dead})
    service = OfficialSearchService(providers=[provider], fetch_body=True, timeout=5.0)
    run = _run(service, _results(6))

    statuses = {source.url: source.body_status.value for source in run.sources}
    assert statuses[dead] == "failed"
    assert sum(1 for state in statuses.values() if state == "fetched") == 5
    assert any(warning.startswith("body_failed:") for warning in run.warnings)


def test_parallel_fetch_keeps_the_cross_step_url_dedup():
    """A URL the caller already read is not fetched again, parallel or not."""
    provider = FakeFetchProvider()
    service = OfficialSearchService(providers=[provider], fetch_body=True, timeout=5.0)
    results = _results(4)
    run = _run(service, results, exclude_urls=[results[0].url])

    assert results[0].url not in provider.calls
    assert len(provider.calls) == 3
    assert all(source.url != results[0].url for source in run.sources)


def test_fetch_body_false_still_sends_nothing():
    provider = FakeFetchProvider()
    service = OfficialSearchService(providers=[provider], fetch_body=False, timeout=5.0)
    run = _run(service, _results(3))

    assert provider.calls == []
    assert all(source.body_status.value != "fetched" for source in run.sources)
