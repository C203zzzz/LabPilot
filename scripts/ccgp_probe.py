#!/usr/bin/env python
"""Throttled, restartable probe for search.ccgp.gov.cn (中国政府采购网).

Why this file exists
--------------------
``设计规范-中国政府采购网正文检索修复.md`` §5 lists four items that were never
measured. Measuring them means sending real requests to a site that rate-limits
by *burst*, so every request has to be worth its cost and every answer has to be
kept on disk as raw output.

Rules this script enforces, so a re-run cannot accidentally become a flood:

* one request at a time, a fixed ``--sleep`` floor between any two requests;
* a ``--batch`` request ceiling, then ``--batch-pause`` seconds of silence;
* the site's own stop word (``频繁访问``) aborts the *whole run* immediately --
  the script never retries, never sleeps-and-continues, never rotates identity;
* every raw payload is written to ``--out`` as UTF-8 HTML, and every request is
  appended to ``--out/requests.jsonl`` with its exact query string, so the
  numbers in the write-up can be re-derived offline without touching the site.

Usage
-----
    python scripts/ccgp_probe.py --out tmp/ccgp --sleep 8 \
        --job tmp/ccgp/job1.jsonl

where each non-empty line of the job file is a JSON object::

    {"label": "a1", "kw": "离心机"}
    {"label": "b2", "kw": "DW-86L578S", "searchtype": 1}
    {"label": "c1", "url": "http://www.ccgp.gov.cn/cggg/.../tX.htm"}

``url`` bypasses search and fetches the page directly (detail-page probes).
Any other key becomes a query-parameter override (``page_index``, ``timeType``,
``start_time``, ``end_time``, ``bidType``, ``dbselect``, ``buyerName`` ...).
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request

ENDPOINT = "http://search.ccgp.gov.cn/bxsearch"

#: Same browser profile the product's CCGPSearchProvider sends. Kept identical
#: on purpose: a probe that gets a different answer because it announced itself
#: as a bot would measure the probe, not the site.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "http://search.ccgp.gov.cn/",
}

#: Parameters the product adapter sends, in the same shape.
DEFAULT_PARAMS = {
    "searchtype": "2",
    "page_index": "1",
    "bidSort": "0",
    "buyerName": "",
    "projectId": "",
    "pinMu": "0",
    "bidType": "0",
    "dbselect": "bidx",
    "kw": "",
    "timeType": "6",
}


def build_search_url(kw: str, *, page: int = 1, window_days: int = 365,
                     overrides: dict[str, str] | None = None) -> str:
    end = dt.datetime.now()
    start = end - dt.timedelta(days=window_days)
    params = dict(DEFAULT_PARAMS)
    params["page_index"] = str(page)
    params["kw"] = kw
    params["start_time"] = start.strftime("%Y:%m:%d")
    params["end_time"] = end.strftime("%Y:%m:%d")
    if overrides:
        for key, value in overrides.items():
            params[str(key)] = str(value)
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def fetch(url: str, timeout: float = 20.0) -> tuple[int, bytes, str]:
    """One GET. Returns (status, payload, error). Never retries."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(16 * 1024 * 1024 + 1), ""
    except urllib.error.HTTPError as exc:  # noqa: PERF203 - probe reports it
        return int(exc.code), exc.read() or b"", f"HTTPError {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - the wording is the evidence
        return 0, b"", f"{type(exc).__name__}: {exc}"


COUNT_PATTERNS = (
    re.compile(r"共\s*([\d,]+)\s*条"),
    re.compile(r"找到\s*([\d,]+)"),
    re.compile(r"([\d,]{1,12})\s*条(?:结果|记录|公告|信息)?"),
)


def total_counts(html: str) -> list[dict[str, str]]:
    """Every 'N 条'-shaped number on the page, with a little context.

    Deliberately generous: the point of the first probe is to find out which
    element is the authoritative total, so nothing is assumed up front.
    """
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in COUNT_PATTERNS:
        for match in pattern.finditer(html):
            context = re.sub(r"\s+", " ", html[max(0, match.start() - 70):match.end() + 20])
            key = (match.group(1), context[:60])
            if key in seen:
                continue
            seen.add(key)
            found.append({"num": match.group(1), "context": context})
    return found


TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", value)).strip()


def placeholder_page(html: str) -> bool:
    """True for the site's ~500-byte neutral stand-in page.

    Under load the endpoint sometimes answers 200 with a stub that has no form,
    no result list and no count at all::

        <p ...>您正在访问中国政府采购网搜索平台</p>

    It is not a search result and not the explicit rate-limit page either, so a
    run that silently counted it as "0 hits" would record a false negative.
    Observed to be transient: an identical retry minutes later returned the real
    page (see the write-up), which is why the probe can retry it once.
    """
    return "您正在访问中国政府采购网搜索平台" in html and len(html) < 4000


def pager_size(html: str) -> int | None:
    """``Pager({ size: N, current: M ...})`` -- the second server-side counter.

    Measured on 2026-09-23: ``N`` counts result *pages* (20 rows each), not
    rows -- ``kw=离心机&page_index=14`` still returned 20 rows while
    ``共找到`` said 4840 = 242 x 20. ``result_total`` is the field to quote;
    this one is kept because it is a second, independent server-side number.
    """
    match = re.search(r"Pager\(\{\s*size:\s*(\d+)", html)
    return int(match.group(1)) if match else None


def result_total(html: str) -> int | None:
    """``共找到 <span>N</span> 条内容`` -- the authoritative match count.

    This is the number the site itself prints, in records, together with the
    mode it used ("全文检索" / "标题检索") and the date window it applied. A
    write-up that quotes a row cap instead of this number is measuring its own
    ``limit`` parameter.
    """
    match = re.search(r"共找到\s*<span[^>]*>\s*([\d,]+)\s*</span>\s*条", html)
    return int(match.group(1).replace(",", "")) if match else None


def result_mode(html: str) -> str:
    """The mode word the page prints right before its count.

    The result header reads ``关键字： <kw> 全文检索 共找到 <span>N</span> 条内容``
    (or ``标题检索`` for ``searchtype=1``), so the word is read from its own
    position instead of from a channel-list link elsewhere on the page -- a
    first attempt matched the nav link "站内资讯" and mislabelled every page.
    """
    match = re.search(r"(全文检索|标题检索)\s*共找到", html)
    return match.group(1) if match else ""


def result_window(html: str) -> str:
    match = re.search(r"查询日期从\s*<span[^>]*>\s*([\d\-:. ]+?)\s*</span>\s*到\s*<span[^>]*>\s*([\d\-:. ]+?)\s*</span>", html)
    return f"{match.group(1)}..{match.group(2)}" if match else ""


def list_rows(html: str) -> list[dict[str, str]]:
    """Rows of the result list, read the same way the product parser reads them."""
    rows: list[dict[str, str]] = []
    for block in re.finditer(
        r'<ul[^>]*class="[^"]*vT-srch-result-list-bid[^"]*"[^>]*>(.*?)</ul>',
        html, re.S | re.I,
    ):
        for item in re.finditer(r"<li[^>]*>(.*?)</li>", block.group(1), re.S | re.I):
            chunk = item.group(1)
            anchor = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', chunk, re.S | re.I)
            if not anchor:
                continue
            rows.append({
                "url": anchor.group(1),
                "title": strip_tags(anchor.group(2)),
                "date": (re.search(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})", strip_tags(chunk)) or [""])[0]
                if re.search(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})", strip_tags(chunk)) else "",
                "strong": " | ".join(strip_tags(m) for m in re.findall(r"<strong[^>]*>(.*?)</strong>", chunk, re.S | re.I)),
                "matched": [strip_tags(m) for m in re.findall(r'<font\s+color=["\']?red["\']?[^>]*>(.*?)</font>', chunk, re.S | re.I)],
                "text": strip_tags(chunk)[:400],
            })
    return rows


#: Note the optional query string: the province portals link attachments as
#: ``....zip?accessCode=...``, and a version of this pattern that required the
#: extension to be followed directly by the closing quote reported zero links
#: on pages that plainly had five of them. Caught by re-reading the same files
#: with a second regex.
ATTACH_RE = re.compile(
    r'href="([^"]+\.(?:zip|rar|pdf|doc|docx|xls|xlsx|7z)(?:\?[^"]*)?)"|href="([^"]*(?:attach|fujian|upload)[^"]*)"',
    re.I,
)


def attachment_links(html: str) -> list[str]:
    links: list[str] = []
    for match in ATTACH_RE.finditer(html):
        links.append(match.group(1) or match.group(2))
    return links


def form_controls(html: str) -> dict[str, list[str]]:
    """Name -> option values for the search form's hidden/select controls.

    The mode switch is a hidden input, so the parameters are read from whatever
    the live page ships rather than from a guess.
    """
    controls: dict[str, list[str]] = {}
    for match in re.finditer(r"<input[^>]*>", html, re.I):
        tag = match.group(0)
        name = re.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        kind = re.search(r'type="([^"]*)"', tag)
        controls.setdefault(name.group(1), []).append(
            f'{kind.group(1) if kind else "?"}={value.group(1) if value else ""}'
        )
    for match in re.finditer(r'<select[^>]*name="([^"]*)"[^>]*>(.*?)</select>', html, re.S | re.I):
        options = [strip_tags(o) + f'[value={v}]' for v, o in
                   ((re.search(r'value="([^"]*)"', o), o) for o in re.findall(r"<option[^>]*>.*?</option>", match.group(2), re.S | re.I))
                   if v]
        controls.setdefault(match.group(1), []).extend(options)
    return controls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="directory for raw payloads + log")
    parser.add_argument("--job", required=True, help="JSONL job file")
    parser.add_argument("--sleep", type=float, default=8.0, help="seconds between any two requests")
    parser.add_argument("--batch", type=int, default=12, help="requests per batch")
    parser.add_argument("--batch-pause", type=float, default=180.0, help="seconds of silence between batches")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retry", type=int, default=1,
                        help="extra attempts for a transient answer (timeout or the neutral stub page)")
    parser.add_argument("--retry-sleep", type=float, default=45.0,
                        help="seconds to wait before such a retry")
    parser.add_argument("--bare", action="store_true",
                        help="send only kw (+overrides) instead of the adapter's full parameter set")
    args = parser.parse_args(argv)

    out = __import__("pathlib").Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "requests.jsonl"

    jobs: list[dict] = []
    # utf-8-sig: job files written by Windows PowerShell carry a BOM.
    for line in open(args.job, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        jobs.append(json.loads(line))

    started = dt.datetime.now().isoformat(timespec="seconds")
    print(f"# run start {started}  sleep={args.sleep}s batch={args.batch} pause={args.batch_pause}s")

    for index, job in enumerate(jobs):
        label = str(job.get("label") or f"q{index + 1:03d}")
        kw = str(job.get("kw") or "")
        if job.get("url"):
            url = str(job["url"])
        else:
            overrides = {k: v for k, v in job.items() if k not in {"label", "kw", "url", "page"}}
            if args.bare:
                params = {"kw": kw}
                params.update({k: str(v) for k, v in overrides.items()})
                url = ENDPOINT + "?" + urllib.parse.urlencode(params)
            else:
                url = build_search_url(kw, page=int(job.get("page", 1)), overrides=overrides)

        if index:
            time.sleep(args.sleep)
            if args.batch and index % args.batch == 0:
                print(f"# batch pause {args.batch_pause}s after {index} requests", flush=True)
                time.sleep(args.batch_pause)

        stamp = dt.datetime.now().isoformat(timespec="seconds")
        status, payload, error = fetch(url, args.timeout)
        html = payload.decode("utf-8", errors="replace")
        # A timeout or the neutral stub is the site asking for a pause, not a
        # result. Wait a *longer* interval and try the same URL once; a stub
        # counted as "0 hits" would be a fabricated measurement.
        attempt = 0
        while attempt < args.retry and (
            error or placeholder_page(html)
        ) and "频繁访问" not in html:
            attempt += 1
            reason = error or "neutral stub page"
            print(f"    transient ({reason}); waiting {args.retry_sleep}s, retry {attempt}/{args.retry}", flush=True)
            time.sleep(args.retry_sleep)
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            status, payload, error = fetch(url, args.timeout)
            html = payload.decode("utf-8", errors="replace")
        limited = "频繁访问" in html or "访问过于频繁" in html
        counts = total_counts(html)
        rows = list_rows(html)
        attach = attachment_links(html)
        record = {
            "ts": stamp, "label": label, "kw": kw, "url": url, "status": status,
            "bytes": len(payload), "error": error, "rate_limited": limited,
            "placeholder": placeholder_page(html), "attempts": attempt + 1,
            "pager_size": pager_size(html),
            "total": result_total(html),
            "mode": result_mode(html),
            "window": result_window(html),
            "counts": counts[:12], "row_count": len(rows),
            "rows": [{"title": r["title"], "url": r["url"], "date": r["date"],
                      "strong": r["strong"], "matched": r["matched"]} for r in rows],
            "attachment_links": attach[:12],
        }
        payload_path = out / f"{index + 1:03d}_{label}.html"
        payload_path.write_bytes(payload)
        record["payload"] = payload_path.name
        if label.startswith("form"):
            record["form_controls"] = form_controls(html)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        head = f"[{index + 1}/{len(jobs)}] {label} kw={kw!r} status={status} bytes={len(payload)} rows={len(rows)} pager={record['pager_size']} total={record['total']} mode={record['mode']}"
        if limited:
            head += "  RATE_LIMIT_PAGE"
        if record["placeholder"]:
            head += "  NEUTRAL_STUB"
        print(head, flush=True)
        if record["window"]:
            print(f"    window {record['window']}", flush=True)
        for item in counts[:6]:
            print(f"    count {item['num']:>8}  …{item['context'][:110]}", flush=True)
        for row in rows[:3]:
            print(f"    row   {row['date']}  {row['title'][:64]}  matched={row['matched']}", flush=True)
        if attach:
            print(f"    attach {attach[:4]}", flush=True)
        if error:
            print(f"    error {error}", flush=True)
        if label.startswith("form") and record.get("form_controls"):
            for name, values in record["form_controls"].items():
                print(f"    form  {name} = {values[:12]}", flush=True)

        if limited:
            print(f"# STOP: site returned its rate-limit page at request {index + 1}; "
                  f"no further request in this run. {dt.datetime.now().isoformat(timespec='seconds')}",
                  flush=True)
            return 2

    print(f"# run end {dt.datetime.now().isoformat(timespec='seconds')}  requests={len(jobs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
