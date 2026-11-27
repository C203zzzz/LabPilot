"""Jina Reader integration for web content extraction.

Jina Reader (r.jina.ai) is a free service that extracts clean markdown content
from web pages, handling JavaScript rendering, complex layouts, and various
encoding issues. It serves as a higher-success-rate alternative to direct
urllib fetching for public web pages.
"""
from __future__ import annotations

import urllib.request
import urllib.parse
from typing import TypedDict


class JinaMetadata(TypedDict):
    """Metadata returned alongside Jina Reader content."""
    status_code: int
    jina_used: bool
    content_length: int
    original_url: str


def fetch_with_jina(url: str, timeout: float = 10.0) -> tuple[str, JinaMetadata]:
    """Fetch web page content through Jina Reader.

    Args:
        url: Target URL to fetch (must start with http:// or https://)
        timeout: Request timeout in seconds

    Returns:
        Tuple of (markdown_text, metadata_dict)

    Raises:
        ValueError: If URL is invalid or response too large
        urllib.error.URLError: If network request fails
        urllib.error.HTTPError: If Jina Reader returns non-2xx status
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("Invalid URL scheme (must be http or https)")

    # Validate the original target before handing it to the hosted reader.
    # Scheme-only validation would let an untrusted search result make Jina
    # fetch localhost, link-local, or DNS-resolved private addresses on our
    # behalf, bypassing the direct reader's network policy.
    from backend.services.research_sources import network_url
    network_url(url)

    # Jina Reader endpoint: prepend r.jina.ai/ to any URL
    jina_url = f"https://r.jina.ai/{url}"

    request = urllib.request.Request(
        jina_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/plain, text/markdown, */*",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        # Jina Reader returns markdown-formatted text
        # Limit response size to prevent memory exhaustion
        MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10MB
        content = response.read(MAX_RESPONSE_SIZE + 1)

        if len(content) > MAX_RESPONSE_SIZE:
            raise ValueError(f"Response too large (>{MAX_RESPONSE_SIZE} bytes)")

        text = content.decode("utf-8", errors="replace")

        if not text.strip():
            raise ValueError("Jina Reader returned empty content")

        metadata: JinaMetadata = {
            "status_code": response.status,
            "jina_used": True,
            "content_length": len(text),
            "original_url": url,
        }

        return text, metadata
