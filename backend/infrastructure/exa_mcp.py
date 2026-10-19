"""Bounded Exa MCP HTTP transport; keyless by default, optional key on request.

The hosted endpoint supports stateless tools/call requests over Streamable HTTP.
JSON and SSE response forms are both part of that transport contract.

``EXA_API_KEY`` is optional.  When the process environment carries a non-blank
value, the request also sends ``Authorization: Bearer <key>`` so the caller
spends its own Exa quota; when it is absent or blank the public keyless endpoint
is used exactly as before.  The key is never logged, never placed in the URL,
and never echoed in an exception message.
"""
from __future__ import annotations

import json
import ipaddress
import math
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from backend.domain.search import normalize_url

ENDPOINT = "https://mcp.exa.ai/mcp"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_QUERY_CHARS = 4000
MAX_RESULTS = 100
API_KEY_ENV = "EXA_API_KEY"
MAX_API_KEY_CHARS = 512


def _api_key(environ=None) -> str:
    """Return the optional Exa API key, or "" to use the keyless public endpoint.

    Read lazily from the process environment on every request, so a restarted
    backend picks up a Windows user-level variable.  A value that is absent,
    empty or only whitespace means "no key" and is never an error.  A value that
    cannot be an HTTP header value (control characters, non-ASCII, oversized) is
    refused with a message that deliberately omits it -- echoing the value would
    put the key into logs and tracebacks.
    """
    source = os.environ if environ is None else environ
    raw = source.get(API_KEY_ENV)
    if not isinstance(raw, str):
        return ""
    key = raw.strip()
    if not key:
        return ""
    if len(key) > MAX_API_KEY_CHARS or any(not 0x21 <= ord(char) <= 0x7E for char in key):
        raise ValueError("Exa MCP API key is malformed")
    return key


def _bounded_limit(value, default: int = 10) -> int:
    """Coerce an untrusted result count to the finite protocol range."""
    try:
        if isinstance(value, bool):
            raise ValueError
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(1, min(value, MAX_RESULTS))


def _bounded_timeout(value, default: float = 12.0) -> float:
    """Prevent accidental infinite or excessively long socket waits."""
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        timeout = default
    if not math.isfinite(timeout):
        timeout = default
    return max(0.1, min(timeout, 60.0))


def decode_rpc_response(payload: bytes):
    try:
        text = payload.decode("utf-8-sig")
    except (AttributeError, UnicodeError) as exc:
        raise ValueError("Exa MCP response is not valid UTF-8") from exc
    try:
        message = json.loads(text)
    except ValueError:
        message = None
        # SSE can contain comments/notifications before the matching result.
        for event in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if not data:
                continue
            try:
                candidate = json.loads(data)
            except (TypeError, ValueError) as exc:
                raise ValueError("Exa MCP returned malformed SSE data") from exc
            if isinstance(candidate, dict) and candidate.get("id") == 1:
                message = candidate
                break
    if not isinstance(message, dict) or message.get("id") != 1:
        raise ValueError("Exa MCP response did not match the request")
    if "error" in message:
        raise ValueError("Exa MCP returned a protocol error")
    result = message.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("Exa MCP returned an invalid tool result")
    return result


def search_exa(query, timeout: float = 12.0):
    """Return the original MCP tool result, without promoting snippets to facts."""
    text = getattr(query, "text", "")
    if not isinstance(text, str):
        raise ValueError("Exa MCP query must be text")
    text = text.strip()
    if not text:
        return {"content": []}
    if len(text) > MAX_QUERY_CHARS:
        raise ValueError("Exa MCP query exceeds the size limit")
    limit = _bounded_limit(getattr(query, "limit", 10))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "web_search_exa", "arguments": {
            "query": text, "numResults": limit, "type": "auto",
        },
    }}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0 (compatible; LabPilot/1.0)",
    })
    key = _api_key()
    if key:
        # Header form rather than ``?exaApiKey=``: ENDPOINT stays an exact
        # constant, and the key can never ride inside a URL that an exception
        # object (``HTTPError.url``) or a test-failure diff would print.
        request.add_header("Authorization", f"Bearer {key}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("Exa MCP endpoint redirect refused")

    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=_bounded_timeout(timeout)) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Error bodies can echo queries or service configuration; do not expose them.
        raise ValueError(f"Exa MCP HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError("Exa MCP request failed") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Exa MCP response exceeds the size limit")
    return decode_rpc_response(payload)


def parse_search_results(result, limit: int = 10) -> list[dict[str, str]]:
    """Extract explicit URL leads from Exa's human-readable MCP tool output.

    The MCP tool currently returns one or more text blocks containing records
    headed by ``Title:`` and ``URL:``.  This parser deliberately does not infer
    URLs from prose or promote highlights to evidence.
    """
    if not isinstance(result, dict):
        return []
    blocks = result.get("content", [])
    if not isinstance(blocks, list):
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    result_limit = _bounded_limit(limit)

    def append_record(title: str, highlights: list[str], raw_url: str) -> bool:
        """Validate and append one URL lead, returning whether cap is met."""
        try:
            raw_host = (urlsplit(raw_url).hostname or "").rstrip(".").casefold()
            if raw_host == "localhost" or raw_host.endswith((".localhost", ".local")):
                return False
            try:
                address = ipaddress.ip_address(raw_host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                return False
            canonical = normalize_url(raw_url)
            parts = urlsplit(canonical)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password:
                return False
        except (TypeError, ValueError):
            return False
        if canonical not in seen:
            seen.add(canonical)
            rows.append({
                "url": canonical,
                "title": title.strip()[:1000],
                "snippet": " ".join(highlights).strip()[:2000],
            })
        return len(rows) >= result_limit

    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text", ""))
        lines = text.splitlines()
        current_title = ""
        current_url = ""
        current_highlights: list[str] = []
        in_highlights = False

        def flush() -> bool:
            nonlocal current_title, current_url, current_highlights, in_highlights
            hit_limit = bool(current_url and append_record(current_title, current_highlights, current_url))
            current_title = ""
            current_url = ""
            current_highlights = []
            in_highlights = False
            return hit_limit

        for line in lines:
            stripped = line.strip()
            if stripped.casefold().startswith("title:"):
                if flush():
                    return rows
                current_title = stripped.split(":", 1)[1].strip()
                continue
            if stripped.casefold().startswith("highlights:"):
                in_highlights = True
                remainder = stripped.split(":", 1)[1].strip()
                if remainder:
                    current_highlights.append(remainder)
                continue
            match = re.match(r"^URL:\s*(https?://\S+)", stripped, flags=re.I)
            if match:
                # Preserve a previous lead if a malformed block contains
                # multiple URL lines without a new Title separator.
                if current_url and flush():
                    return rows
                current_url = match.group(1).rstrip(")],;>\"'")
                in_highlights = False
                continue
            if in_highlights and stripped and not stripped.startswith("---"):
                current_highlights.append(stripped)
        if flush():
            return rows
    return rows


def search_exa_results(query, timeout: float = 12.0) -> list[dict[str, str]]:
    """Run the Exa MCP search (optional ``EXA_API_KEY``) and return explicit URL leads."""
    return parse_search_results(search_exa(query, timeout=timeout), limit=getattr(query, "limit", 10))
