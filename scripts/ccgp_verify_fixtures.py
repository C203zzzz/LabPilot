#!/usr/bin/env python
"""Re-derive every claim in ``设计说明`` from saved pages.

Offline: reads only files under ``tests/fixtures/ccgp-index-study-2026-09-23/``.
No network call is made, so anyone can reproduce the verdicts without spending a
request against a rate-limited site.

    python scripts/ccgp_verify_fixtures.py

For each fixture it prints the site's own fields -- the mode word
(``全文检索``/``标题检索``), the authoritative count (``共找到 N 条内容``), the
date window the server applied, the row count and the tokens the site
highlighted -- and compares them with the numbers written in the document. A
mismatch is printed as FAIL and makes the exit code 1.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "ccgp-index-study-2026-09-23"

_spec = importlib.util.spec_from_file_location("ccgp_probe", ROOT / "scripts" / "ccgp_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

#: fixture -> expected fields. ``None`` means "do not check". ``highlight`` is
#: the first row's ``<font color=red>`` text; ``attachments`` is the number of
#: direct file links on the page (list pages must have zero).
EXPECTED: dict[str, dict] = {
    # --- the site's own counters, and the two search modes -------------------
    "list-bio-searchtype2.html": dict(mode="全文检索", total=1526, pager=77, rows=20, attachments=0),
    "list-bio-searchtype1.html": dict(mode="标题检索", total=178, pager=9, rows=20, attachments=0),
    # --- model tokenisation --------------------------------------------------
    "list-TBSPX.html": dict(total=1, pager=1, rows=1, highlight=["TBSPX"]),
    "list-TBSPX-150BIII.html": dict(total=0, pager=None, rows=0),
    "list-TBSPX-space-150BIII.html": dict(total=0, pager=None, rows=0),
    "list-150BIII.html": dict(total=0, pager=None, rows=0),
    "list-SPX-150BIII.html": dict(total=0, pager=None, rows=0),
    "list-BIII.html": dict(total=2, pager=1, rows=2),
    "list-TBSPX-space-BIII.html": dict(total=0, pager=None, rows=0),
    "list-TB101-2DB.html": dict(total=0, pager=None, rows=0),
    "list-TBSYG-A2-6.html": dict(total=0, pager=None, rows=0),
    "list-TBMF-7-12P.html": dict(total=0, pager=None, rows=0),
    "list-DW-86L578S.html": dict(total=2, pager=1, rows=2, highlight=["DW-86L578S"]),
    "list-86L578S.html": dict(total=2, pager=1, rows=2, highlight=["86L578S"]),
    "list-86L578.html": dict(total=1, pager=1, rows=1, highlight=["86L578"]),
    "list-DW-86L578.html": dict(total=0, pager=None, rows=0),
    "list-DW-86.html": dict(total=0, pager=None, rows=0),
    "list-DW86L578S.html": dict(total=0, pager=None, rows=0),
    "list-DW-space-86L578S.html": dict(total=2, pager=1, rows=2, highlight=["DW", "86L578S"]),
    "list-DW-space-578S.html": dict(total=0, pager=None, rows=0),
    "list-ZQLY-prefix.html": dict(total=124, pager=7, rows=20),
    "list-DW-two-letter-prefix.html": dict(total=920, pager=46, rows=20),
    "list-1574.html": dict(total=116, pager=6, rows=20),
    "list-taisite.html": dict(total=167, pager=9, rows=20),
    "list-zhichu.html": dict(total=66, pager=4, rows=20),
    "list-ZQTY-50.html": dict(total=2, pager=1, rows=2, highlight=["ZQTY-50"]),
    "list-anquangui.html": dict(total=1689, pager=85, rows=20),
    # --- term joining: '-' behaves exactly like a space ----------------------
    "list-bio-minus-shiyanshi.html": dict(total=103, pager=6, rows=20),
    "list-bio-and-shiyanshi.html": dict(total=103, pager=6, rows=20),
    # --- coverage: types, central/local, windows ----------------------------
    "list-bio-bidSort1-central.html": dict(total=208, pager=11, rows=20),
    "list-bio-bidSort2-local.html": dict(total=1318, pager=66, rows=20),
    "list-caigou-bidType5-prequal.html": dict(total=161, pager=9, rows=20),
    "list-caigou-bidType6-invite.html": dict(total=61, pager=4, rows=20),
    "list-bio-window-366d.html": dict(total=1531, pager=77, rows=20, window="2025-09-22..2026-09-23"),
    "list-bio-window-370d.html": dict(total=921, pager=47, rows=20, window="2025-09-18..2026-09-23"),
    "list-lixinji-window-400d.html": dict(total=2921, pager=147, rows=20, window="2025-08-19..2026-09-23"),
    "list-bio-window-730d-zero.html": dict(total=0, pager=None, rows=0, window="2024-09-23..2026-09-23"),
    "list-bio-window-2024-03-to-2025-03.html": dict(total=1638, pager=82, rows=20, window="2024-03-23..2025-03-23"),
    "list-bio-window-2023-09-to-2024-09.html": dict(total=1333, pager=67, rows=20, window="2023-09-23..2024-09-23"),
    "list-bio-window-2020H1-clamped.html": dict(total=2, pager=1, rows=2, window="2026-09-23..2026-09-23"),
    "list-bio-window-2021-clamped.html": dict(total=2, pager=1, rows=2, window="2026-09-23..2026-09-23"),
    "list-bio-window-2019-clamped.html": dict(total=2, pager=1, rows=2, window="2026-09-23..2026-09-23"),
    "list-bio-window-2022-09-rejected.html": dict(total=0, pager=None, rows=0, window="2026-09-23..2023-09-23"),
    # --- the neutral stub page, kept as a sample of the failure mode ---------
    "stub-c3-bio-2004-to-2010.html": dict(mode="", total=None, rows=0, stub=True),
    "stub-c4-bio-2010-to-now.html": dict(mode="", total=None, rows=0, stub=True),
    # --- attachments --------------------------------------------------------
    "detail-guangdong-qinggong-TBSPX-150BIII.html": dict(mode="", total=None, rows=0, attachments=4),
    "detail-zhujiang-DW-86L578S.html": dict(mode="", total=None, rows=0, attachments=5),
    "detail-chengde-MD-86L578.html": dict(mode="", total=None, rows=0, attachments=1),
    "detail-zygg-central-empty-href.html": dict(mode="", total=None, rows=0, attachments=0),
}


def main() -> int:
    if not FIXTURES.is_dir():
        print(f"fixtures missing: {FIXTURES}")
        return 1
    failures: list[str] = []
    print(f"fixtures: {FIXTURES}")
    print(f"{'fixture':52} {'mode':6} {'total':>6} {'pager':>6} {'rows':>4} {'attach':>6} {'window':23} highlight")
    for path in sorted(FIXTURES.glob("*.html")):
        html = path.read_text(encoding="utf-8", errors="replace")
        rows = probe.list_rows(html)
        facts = {
            "mode": probe.result_mode(html),
            "total": probe.result_total(html),
            "pager": probe.pager_size(html),
            "rows": len(rows),
            "window": probe.result_window(html),
            "highlight": rows[0]["matched"] if rows else [],
            "attachments": len(probe.attachment_links(html)),
            "placeholder": probe.placeholder_page(html),
        }
        print(f"{path.name[:52]:52} {facts['mode'] or '-':6} {str(facts['total']):>6} "
              f"{str(facts['pager']):>6} {facts['rows']:>4} {facts['attachments']:>6} "
              f"{facts['window'][:23]:23} {facts['highlight'][:3]}")
        expected = EXPECTED.get(path.name)
        if expected is None:
            print(f"    (no expectation recorded for {path.name})")
            continue
        for key in ("mode", "total", "pager", "rows", "window", "highlight", "attachments"):
            if key not in expected:
                continue
            if facts[key] != expected[key]:
                failures.append(f"{path.name}: {key} = {facts[key]!r}, document says {expected[key]!r}")
        if facts["placeholder"] and not expected.get("stub"):
            failures.append(f"{path.name}: is the neutral stub page, not a result")
        if expected.get("stub") and not facts["placeholder"]:
            failures.append(f"{path.name}: expected the neutral stub page, got a result page")
    # A list page must never carry an attachment link: that is the whole point
    # of "attachments only exist on the detail page".
    for path in sorted(FIXTURES.glob("list-*.html")):
        found = probe.attachment_links(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            failures.append(f"{path.name}: list page carries {len(found)} attachment link(s): {found[:2]}")

    # Province tags: the document claims 26 distinct provinces appear in the
    # saved page-1 samples, which is the evidence for "local notices are in the
    # index". Re-derive it rather than trusting the sentence.
    provinces: set[str] = set()
    sampled_rows = 0
    for path in sorted(FIXTURES.glob("list-*.html")):
        for row in probe.list_rows(path.read_text(encoding="utf-8", errors="replace")):
            sampled_rows += 1
            text = probe.strip_tags(row["text"])
            kind = probe.strip_tags(row["strong"]) if row["strong"] else ""
            index = text.rfind(kind) if kind else -1
            rest = text[index + len(kind):] if index >= 0 else ""
            fields = [part.strip() for part in rest.split("|") if part.strip()]
            if fields:
                provinces.add(fields[0])
    print(f"\nprovince tags in the saved samples: {len(provinces)} distinct over {sampled_rows} rows")
    print("  " + " ".join(sorted(provinces)))
    if len(provinces) < 26:
        failures.append(f"province sample shrank: {len(provinces)} distinct (< 26)")

    # The product's own parser must read these same files the same way; this is
    # the cross-check that the probe's regexes are not measuring something else.
    try:
        sys.path.insert(0, str(ROOT))
        from backend.infrastructure.search_providers import CCGPSearchProvider
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        print(f"\n(product parser import skipped: {type(exc).__name__}: {exc})")
    else:
        print("\nproduct parser cross-check (CCGPSearchProvider._parse_ccgp_results):")
        for name, expected_rows in (
            ("list-DW-86L578S.html", 2), ("list-TBSPX.html", 1),
            ("list-DW-space-86L578S.html", 2), ("list-bio-searchtype2.html", 20),
        ):
            html = (FIXTURES / name).read_bytes()
            try:
                parsed = CCGPSearchProvider._parse_ccgp_results(html, 50)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: product parser raised {type(exc).__name__}: {exc}")
                continue
            urls = [row["url"] for row in parsed]
            print(f"  {name:40} rows={len(parsed)} first={urls[0] if urls else ''}")
            if len(parsed) != expected_rows:
                failures.append(f"{name}: product parser returned {len(parsed)} rows, expected {expected_rows}")

    log = FIXTURES / "requests.jsonl"
    if log.is_file():
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        limited = [r for r in records if r.get("rate_limited")]
        errors = [r for r in records if r.get("error")]
        http500 = [r for r in records if r.get("status") == 500]
        stubs = sorted(FIXTURES.glob("stub-*.html"))
        for stub in stubs:
            if not probe.placeholder_page(stub.read_text(encoding="utf-8", errors="replace")):
                failures.append(f"{stub.name}: is not the neutral stub page")
        # Counts that come straight out of the request log; the stub pages are
        # counted from the fixtures because the log only grew that field later.
        print(f"\nraw request log: {len(records)} requests, "
              f"{len(limited)} rate-limit page(s), {len(http500)} HTTP 500, "
              f"{len(errors)} transport error(s); "
              f"{len(stubs)} neutral stub page(s) kept as fixtures")
        labels = {r["label"] for r in records}
        if not {"c3_bio_2004_2010", "c4_bio_2010_now"} <= labels:
            failures.append("the stub-page requests are missing from requests.jsonl")
    else:
        failures.append("requests.jsonl missing from the fixture set")

    print()
    if failures:
        print(f"FAIL ({len(failures)})")
        for item in failures:
            print("  -", item)
        return 1
    print(f"PASS: every recorded expectation matches the saved pages "
          f"({len(list(FIXTURES.glob('*.html')))} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
