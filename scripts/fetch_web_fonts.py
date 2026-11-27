"""Re-fetch the self-hosted webfaces used by ``frontend/src/styles/index.css``.

The frontend used to load DM Mono / Instrument Serif / Manrope through an
``@import`` on fonts.googleapis.com.  That request both delayed first paint and
let the wordmark face swap in after the start page had already painted, which
re-laid-out "LabPilot" by ~20% mid-animation.  The faces now ship from
``frontend/public/fonts``; this script is how they were obtained, so the set can
be regenerated or extended without reverse-engineering the CDN URLs.

Usage (from the repository root):

    python scripts/fetch_web_fonts.py            # download + print @font-face
    python scripts/fetch_web_fonts.py --check    # verify the checked-in files

The printed ``@font-face`` block is pasted into the font section at the top of
``frontend/src/styles/index.css``; ``index.html`` preloads the two faces the
start page needs before first paint.
"""
from __future__ import annotations

import argparse
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "frontend" / "public" / "fonts"

# Manrope is requested as a variable face (one file per subset) rather than five
# static weights; DM Mono and Instrument Serif only publish static instances.
CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=DM+Mono:wght@400;500"
    "&family=Instrument+Serif"
    "&family=Manrope:wght@200..800"
    "&display=swap"
)
# Chinese-first UI: the cyrillic and vietnamese subsets would only add payload.
KEEP_SUBSETS = ("latin", "latin-ext", "greek")
SLUGS = {"DM Mono": "dm-mono", "Instrument Serif": "instrument-serif", "Manrope": "manrope"}
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


def _fetch(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def _faces() -> list[dict[str, str]]:
    css = _fetch(CSS_URL).decode("utf-8")
    blocks = re.findall(r"/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{(.*?)\}", css, re.S)
    if not blocks:
        raise SystemExit(f"no @font-face blocks parsed from {CSS_URL}")

    faces = []
    for subset, body in blocks:
        if subset not in KEEP_SUBSETS:
            continue
        family = re.search(r"font-family:\s*'([^']+)'", body).group(1)
        weight = re.search(r"font-weight:\s*([\d ]+)", body).group(1).strip()
        style = re.search(r"font-style:\s*(\w+)", body).group(1)
        faces.append(
            {
                "family": family,
                "weight": weight,
                "style": style,
                "unicode_range": re.search(r"unicode-range:\s*([^;]+);", body).group(1).strip(),
                "url": re.search(r"url\((https://[^)]+)\)", body).group(1),
                "file": f"{SLUGS[family]}-{weight.replace(' ', '-')}-{style}-{subset}.woff2",
            }
        )
    return faces


def _declaration(face: dict[str, str]) -> str:
    return (
        "@font-face {\n"
        f"  font-family: '{face['family']}';\n"
        f"  font-style: {face['style']};\n"
        f"  font-weight: {face['weight']};\n"
        "  font-display: swap;\n"
        f"  src: url('/fonts/{face['file']}') format('woff2');\n"
        f"  unicode-range: {face['unicode_range']};\n"
        "}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify that every face is present locally")
    args = parser.parse_args()

    faces = _faces()
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    missing = []
    for face in faces:
        target = FONT_DIR / face["file"]
        if not target.exists():
            if args.check:
                missing.append(face["file"])
                continue
            target.write_bytes(_fetch(face["url"]))
        total += target.stat().st_size

    if missing:
        raise SystemExit("missing webfont files: " + ", ".join(missing))
    if args.check:
        print(f"{len(faces)} faces present in {FONT_DIR} ({total / 1024:.1f} KiB)")
        return

    print("\n".join(_declaration(face) for face in faces))
    print(f"\n{len(faces)} faces, {total / 1024:.1f} KiB -> {FONT_DIR}")


if __name__ == "__main__":
    main()
