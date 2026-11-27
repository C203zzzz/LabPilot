#!/usr/bin/env python
"""Offline anatomy dump for a page saved by ``scripts/ccgp_probe.py``.

Everything here runs on a file already on disk, so it costs no request. The
question it answers first is "which number on this page is the authoritative
total match count", because that is the difference between a row cap and a
coverage claim.

    python scripts/ccgp_inspect.py tmp/ccgp/001_form1.html
"""

from __future__ import annotations

import re
import sys

TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", value)).strip()


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(value)


def main(argv: list[str]) -> int:
    for path in argv[1:]:
        html = open(path, encoding="utf-8", errors="replace").read()
        print(f"\n########## {path}  ({len(html)} chars) ##########")

        # 1. Any number that could be a total.
        numbers: dict[str, list[str]] = {}
        for match in re.finditer(r"([\d,]{1,12})\s*条", html):
            numbers.setdefault(match.group(1), []).append(
                re.sub(r"\s+", " ", html[max(0, match.start() - 60):match.end() + 10])
            )
        show("'N 条' occurrences", {k: v[:3] for k, v in numbers.items()} or "none")

        # 2. Elements whose class/id smells like a counter.
        show("counter-ish markup",
             [re.sub(r"\s+", " ", m.group(0))[:160] for m in
              re.finditer(r'<[^>]*(?:class|id)="[^"]*(?:num|count|total|result|page)[^"]*"[^>]*>[^<]{0,80}', html, re.I)][:25])

        # 3. The pagination block, which usually spells out the page total.
        show("pagination markup",
             [re.sub(r"\s+", " ", m.group(0))[:400] for m in
              re.finditer(r'<(?:div|p|span)[^>]*class="[^"]*(?:pager|page|pag)[^"]*"[^>]*>.*?</(?:div|p|span)>', html, re.S | re.I)][:3])

        # 4. The result list itself.
        blocks = re.findall(r'<ul[^>]*class="[^"]*vT-srch-result-list-bid[^"]*"[^>]*>(.*?)</ul>', html, re.S | re.I)
        show("result <ul> blocks", len(blocks))
        for index, block in enumerate(blocks):
            items = re.findall(r"<li[^>]*>(.*?)</li>", block, re.S | re.I)
            print(f"  block {index}: {len(items)} <li>")
            for item in items[:2]:
                print("   ", re.sub(r"\s+", " ", item)[:700])
            if items:
                print("  strong fields of every row:",
                      [strip_tags(" ".join(re.findall(r"<strong[^>]*>(.*?)</strong>", i, re.S))) for i in items])

        # 5. Attachment-shaped links anywhere on the page.
        show("attachment-shaped hrefs",
             sorted({m.group(0) for m in re.finditer(r'href="[^"]+\.(?:zip|rar|pdf|docx?|xlsx?|7z)"', html, re.I)})[:10] or "none")

        # 6. Where the rows point, as a proxy for announcement type.
        paths: dict[str, int] = {}
        for match in re.finditer(r'href="https?://www\.ccgp\.gov\.cn(/cggg/[^"]+?)t\d+\.htm"', html):
            path = re.sub(r"/\d{6}/?$", "/", match.group(1))
            paths[path] = paths.get(path, 0) + 1
        show("row URL path prefixes", paths)

        # 7. Snippet/highlight markup, which names the token the index matched.
        show("highlight markup",
             sorted({re.sub(r"\s+", " ", m.group(0))[:120] for m in
                     re.finditer(r"<(?:em|font|span)[^>]*>(?:[^<]{0,40})</(?:em|font|span)>", html, re.I)})[:6] or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
