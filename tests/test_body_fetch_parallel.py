"""Bounded parallel body reads on the main research path (offline).

``procurement_research.fetch_source`` reads its bodies through the shared page
reader, one url at a time, and that serial loop was the longest wait left in a
research round. ``fetch_sources`` / ``prefetch_bodies`` now read the network
part of a batch ahead of time with at most ``CCGP_BODY_FETCH_WORKERS`` workers.

What these tests pin down, in the order the risk matters:

* bodies, ``body_status`` and failure attribution stay identical to reading
  them one at a time (including a dead page and a reader that escalates);
* a rate-limit page stops the batch -- nothing further is read, the paused site
  receives no request at all for the rest of the round, and the rate-limit page
  is never taken as an article;
* the batch never reads past the remaining ``fetch`` budget;
* nothing already cached or already on disk is requested again;
* the worker count comes from the shared hard ceiling and cannot exceed it.
"""
import hashlib
import threading
import time

import pytest

from backend.infrastructure.page_reader import PageReadError, PageResult
import backend.infrastructure.search_providers as search_providers
from backend.infrastructure.search_providers import CCGP_BODY_FETCH_WORKERS, ProviderConfigurationError
from backend.repositories.sqlite import SQLiteRepository
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_research import ProcurementResearch, ResearchStopped

URLS = [f"http://notice{index}.example.test/cggg/{index}" for index in range(1, 9)]
DEAD_URL = "http://notice9.example.test/cggg/gone"


class FakeReader:
    """Stands in for ``PublicPageReader.read``; never touches the network."""

    name = "fake_reader"

    def __init__(self, *, delay=0.05, fail_urls=(), escalate_urls=(), rate_limit_urls=(),
                 rate_limit_delay=0.0):
        self.delay = delay
        self.rate_limit_delay = rate_limit_delay
        self.fail_urls = set(fail_urls)
        self.escalate_urls = set(escalate_urls)
        self.rate_limit_urls = set(rate_limit_urls)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls: list[str] = []
        self.paused_until = 0.0

    def read(self, url, timeout=15, on_attempt=None):
        # The real reader charges the caller first (``read`` calls ``on_attempt``
        # before it opens the request), so a budget stop never records a call.
        if on_attempt:
            on_attempt()
        with self.lock:
            paused = time.monotonic() < self.paused_until
        if paused:
            # The real adapter refuses inside ``_request``, before anything is
            # sent: a paused site receives no further request this round.
            raise ProviderConfigurationError("ccgp paused after a rate-limit page")
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.calls.append(url)
        try:
            time.sleep(self.rate_limit_delay if url in self.rate_limit_urls else self.delay)
            if url in self.rate_limit_urls:
                # The adapter arms its own cooldown before raising; mirror that.
                self.paused_until = time.monotonic() + 60.0
                raise ProviderConfigurationError("ccgp rate limited (频繁访问)")
            if url in self.escalate_urls and on_attempt:
                on_attempt()  # http failed, the browser retry spends a second unit
            if url in self.fail_urls:
                raise PageReadError("not_found", 404)
            html = f"<html><body><h1>公告</h1><p>{url} 中标 成交单价 12000 元</p></body></html>"
            return PageResult(html.encode("utf-8"), "text/html; charset=utf-8",
                              f"公告 {url} 中标 成交单价 12000 元",
                              {"reader": "http", "snapshot_kind": "response_bytes", "status_code": 200})
        finally:
            with self.lock:
                self.in_flight -= 1


def _service(tmp_path, name="a", **reader_kwargs):
    reader = FakeReader(**reader_kwargs)
    search = OfficialSearchService(providers=[], fetch_body=False, timeout=5.0)
    search.reader = reader
    repo = SQLiteRepository(tmp_path / name / "app.db")
    return ProcurementResearch(repo, search, None), reader


def _run(service, *, fetch_limit=100):
    run = {"id": "research-1", "task_id": "task-1", "kind": "research", "status": "running",
           "round": 1, "baseline": {"model": "Q10", "brand": "甲厂", "scenario": "容量检测"},
           "sources": [], "gaps": [], "completed_queries": [], "stage_timings": [],
           "usage": {"search": 0, "fetch": 0, "model": 0},
           "limits": {"search": 10, "fetch": fetch_limit, "model": 10},
           "created_at": "2026-09-24T00:00:00+00:00"}
    service.save(run)
    return run


def _items(urls):
    return [{"url": url, "source_type": "unknown"} for url in urls]


def test_a_batch_reads_bodies_in_parallel_within_the_hard_ceiling(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)

    got = service.fetch_sources(run, _items(URLS))

    assert [source["url"] for source in got] == URLS, "the batch must stay in source order"
    assert all(source["body_status"] == "fetched" for source in got)
    assert reader.peak > 1, "bodies were still read one at a time"
    assert reader.peak <= CCGP_BODY_FETCH_WORKERS, reader.peak
    assert run["usage"]["fetch"] == len(URLS)


def test_the_worker_count_comes_from_the_shared_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(search_providers, "CCGP_BODY_FETCH_WORKERS", 2)
    service, reader = _service(tmp_path)
    run = _run(service)

    service.fetch_sources(run, _items(URLS))

    assert reader.peak <= 2


def test_parallel_batch_matches_the_serial_round_entry_by_entry(tmp_path):
    urls = URLS + [DEAD_URL]
    serial_service, serial_reader = _service(tmp_path, "serial")
    serial_run = _run(serial_service)
    serial_out = [serial_service.fetch_source(serial_run, item) for item in _items(urls)]

    batch_service, batch_reader = _service(tmp_path, "batch")
    batch_run = _run(batch_service)
    batch_out = batch_service.fetch_sources(batch_run, _items(urls))

    assert len(serial_out) == len(batch_out)
    for one, other in zip(serial_out, batch_out):
        assert one["url"] == other["url"]
        assert one["body_text"] == other["body_text"]
        assert one["body_status"] == other["body_status"]
        assert one.get("failure_kind") == other.get("failure_kind")
        assert one.get("error_reason") == other.get("error_reason")
        assert one["snapshot"]["sha256"] == other["snapshot"]["sha256"]
    assert serial_run["usage"]["fetch"] == batch_run["usage"]["fetch"]
    assert sorted(serial_reader.calls) == sorted(batch_reader.calls)


def test_a_reader_escalation_spends_the_same_two_units(tmp_path):
    urls = URLS[:4]
    serial_service, _ = _service(tmp_path, "serial", escalate_urls={urls[1]})
    serial_run = _run(serial_service)
    [serial_service.fetch_source(serial_run, item) for item in _items(urls)]

    batch_service, _ = _service(tmp_path, "batch", escalate_urls={urls[1]})
    batch_run = _run(batch_service)
    batch_service.fetch_sources(batch_run, _items(urls))

    assert serial_run["usage"]["fetch"] == len(urls) + 1
    assert batch_run["usage"]["fetch"] == serial_run["usage"]["fetch"]


def test_a_rate_limit_page_stops_the_batch_from_reading_on(tmp_path):
    """撞限速页：同批不再发新请求，限速页也不会被当成正文收下。"""
    service, reader = _service(tmp_path, rate_limit_urls={URLS[1]})
    run = _run(service)
    urls = URLS + [DEAD_URL]

    got = service.fetch_sources(run, _items(urls))

    # The rate-limited read returns first and stops the batch, so only reads
    # already in flight could have reached the site.
    assert set(reader.calls) <= set(URLS[:3]), reader.calls
    assert URLS[1] in reader.calls
    assert got[1]["body_status"] == "failed"
    assert URLS[1] not in {source["url"] for source in got if source["body_status"] == "fetched"}
    assert all(source["body_status"] == "failed" for source in got[1:])


def test_a_paused_reader_sends_nothing_for_the_rest_of_the_round(tmp_path):
    """暂停期内的后续批次一个请求都不发——这就是「整轮停下来」。"""
    service, reader = _service(tmp_path, rate_limit_urls={URLS[1]})
    run = _run(service)

    service.fetch_sources(run, _items(URLS[:3]))
    after_limit = list(reader.calls)
    got = service.fetch_sources(run, _items(URLS[3:]))

    assert reader.calls == after_limit, "a paused site must not receive another request"
    assert all(source["body_status"] == "failed" for source in got)


def test_a_rate_limit_page_batch_is_never_less_careful_than_the_serial_round(tmp_path):
    """撞限速时并行版只会更保守，不会把限速站点的页面当成成功。

    Entry-by-entry equality is asserted for the normal round (no rate-limit
    page) above. Once the site answers with its rate-limit page, a parallel
    batch cannot reproduce the serial order: reads that the serial path had not
    started yet may already be in flight. What must hold either way is that no
    body is reported as fetched after the site refused, and that the parallel
    batch never reports *more* fetched pages than the serial one.
    """
    urls = URLS[:6]
    serial_service, _ = _service(tmp_path, "serial", rate_limit_urls={URLS[1]})
    serial_run = _run(serial_service)
    serial_out = [serial_service.fetch_source(serial_run, item) for item in _items(urls)]

    batch_service, _ = _service(tmp_path, "batch", rate_limit_urls={URLS[1]})
    batch_run = _run(batch_service)
    batch_out = batch_service.fetch_sources(batch_run, _items(urls))

    serial_fetched = {source["url"] for source in serial_out if source["body_status"] == "fetched"}
    batch_fetched = {source["url"] for source in batch_out if source["body_status"] == "fetched"}
    assert batch_fetched <= serial_fetched, (batch_fetched, serial_fetched)
    assert URLS[1] not in serial_fetched and URLS[1] not in batch_fetched
    for url, one, other in zip(urls, serial_out, batch_out):
        if one["body_status"] == "failed":
            assert other["body_status"] == "failed", url


def test_a_rate_limit_page_stops_the_batch_with_the_pool_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(search_providers, "CCGP_BODY_FETCH_WORKERS", 1)
    service, reader = _service(tmp_path, rate_limit_urls={URLS[1]})
    run = _run(service)

    service.fetch_sources(run, _items(URLS))

    assert reader.calls == URLS[:2], reader.calls


def test_the_batch_never_reads_past_the_remaining_fetch_budget(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service, fetch_limit=3)

    with pytest.raises(ResearchStopped):
        service.fetch_sources(run, _items(URLS))

    assert len(reader.calls) == 3, reader.calls
    assert run["usage"]["fetch"] == 3


def test_a_cached_body_is_not_requested_again(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)
    service.fetch_sources(run, _items(URLS[:1]))
    before = list(reader.calls)

    got = service.fetch_sources(run, _items(URLS[:1]))

    assert got[0]["cache_hit"] is True
    assert reader.calls == before, "a cached body must not be read again"
    assert run["usage"]["fetch"] == 1


def test_a_body_stored_on_disk_is_not_requested_again(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)
    service.fetch_sources(run, _items(URLS[:1]))
    key = "body-cache-" + hashlib.sha256(URLS[0].encode()).hexdigest()
    record = service.repo.get("procurement_records", key)
    record["body"]["body_status"] = "failed"  # no longer reusable as-is
    service.repo.put("procurement_records", key, record, task_id=run["task_id"])
    before = list(reader.calls)

    got = service.fetch_sources(run, _items(URLS[:1]))

    assert reader.calls == before, "a stored snapshot must be read from disk, not the network"
    assert got[0]["cache_hit"] is True
    assert got[0]["body_status"] == "fetched"


def test_every_source_in_a_batch_is_read_exactly_once(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)

    service.fetch_sources(run, _items(URLS[:4]))

    assert sorted(reader.calls) == sorted(URLS[:4]), reader.calls
    assert run["usage"]["fetch"] == 4


def test_an_inner_prefetch_cannot_swallow_the_outer_batch(tmp_path):
    """``query``'s loop body prefetches attachments while its own batch is live."""
    service, reader = _service(tmp_path)
    run = _run(service)

    outer = service.prefetch_bodies(run, _items(URLS[:3]))
    service.prefetch_bodies(run, _items(URLS[3:5]))  # nested, like fetch_attachments
    got = [service._fetch_source(run, item, outer.get(item["url"])) for item in _items(URLS[:3])]

    assert all(source["body_status"] == "fetched" for source in got)
    assert sorted(reader.calls) == sorted(URLS[:5]), reader.calls


def test_a_prefetched_read_is_taken_once(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)
    prefetched = service.prefetch_bodies(run, _items(URLS[:1]))

    first = service._fetch_source(run, {"url": URLS[0], "source_type": "unknown"},
                                  prefetched.get(URLS[0]))
    second = service._fetch_source(run, {"url": URLS[0], "source_type": "unknown"},
                                   prefetched.get(URLS[0]))

    assert first["body_status"] == "fetched"
    assert second["cache_hit"] is True, "the same prefetched read must not be spent twice"
    assert run["usage"]["fetch"] == 1
    assert reader.calls == URLS[:1]


def test_a_single_source_batch_is_still_read(tmp_path):
    service, reader = _service(tmp_path)
    run = _run(service)

    got = service.fetch_sources(run, _items(URLS[:1]))

    assert got[0]["body_status"] == "fetched"
    assert reader.calls == URLS[:1]
