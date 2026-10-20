"""M04 search adapters. No credentials or network client is embedded here."""
from __future__ import annotations

import os
import json
import re
import threading
import time
from io import BytesIO, StringIO
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html import unescape
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Sequence

from backend.domain.search import SearchQuery, SearchResult
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.infrastructure.exa_mcp import search_exa_results


class ProviderConfigurationError(RuntimeError):
    """Raised when an optional real provider has no runtime configuration."""


#: Stable wording a caller can match on. A channel failure that is a property of
#: the account or of this client's request shape cannot be fixed by asking again
#: two seconds later, so the orchestrator must be able to tell it apart from a
#: timeout (which is worth its retries) without parsing provider prose.
FATAL_CHANNEL_QUARANTINE_MARKER = 'channel quarantined'

#: HTTP statuses that describe the *account* or the *request shape* rather than
#: this moment, so a retry cannot change the answer.
#:
#: Measured 2026-09-25 against the two channels that failed every task of
#: : ``api.tavily.com/search`` answers 432 with the body
#: ``This request exceeds your plan's set usage limit``, and the deployment the
#: acceptance run pointed ``deepseek_web`` at answers 400 with
#: ``'function' is a required property, expected an object - 'tools.0'``.
#: Both answers were byte-identical on every one of the 30 live requests that
#: run made (15 + 15), which is what "a retry cannot change it" means here.
CHANNEL_FATAL_HTTP_STATUSES = frozenset({400, 401, 402, 403, 432})

#: How long a quarantined channel stays out of the chain, in seconds.
#:
#: Deliberately longer than one research round: the measured acceptance batch
#: spent 55 minutes over 5 tasks and every task re-discovered the same two dead
#: channels, because the streak counter lived on the run. A key, a plan or an
#: endpoint changes only when the process is restarted anyway, so this clock is
#: per process and long enough to cover a whole batch.
CHANNEL_QUARANTINE_SECONDS = 6 * 60 * 60.0

_channel_quarantine_lock = threading.Lock()
#: channel name -> (monotonic deadline, human-readable reason).
_channel_quarantine: dict[str, tuple[float, str]] = {}

_CHANNEL_HTTP_STATUS = re.compile(r'\bHTTP\s+(\d{3})\b')


def channel_quarantine_reason(name: str) -> str | None:
    """Why *name* is out of the chain right now, or ``None`` when it is usable.

    An expired entry is dropped on read: the clock is the only thing that ends a
    quarantine, and nothing has to sweep the mapping.
    """
    with _channel_quarantine_lock:
        entry = _channel_quarantine.get(name)
        if not entry:
            return None
        deadline, reason = entry
        if time.monotonic() >= deadline:
            _channel_quarantine.pop(name, None)
            return None
        return reason


def quarantine_channel(name: str, reason: str, *, seconds: float = CHANNEL_QUARANTINE_SECONDS) -> None:
    """Take *name* out of every later chain in this process, with its reason."""
    with _channel_quarantine_lock:
        _channel_quarantine[name] = (time.monotonic() + float(seconds), str(reason))


def clear_channel_quarantine(name: str | None = None) -> None:
    """Forget one quarantine, or all of them. Tests and explicit recovery use it."""
    with _channel_quarantine_lock:
        if name is None:
            _channel_quarantine.clear()
        else:
            _channel_quarantine.pop(name, None)


def _error_body(exc) -> str:
    """The first 240 characters of an HTTP error body, or the empty string."""
    try:
        payload = exc.read(4000)
    except Exception:  # noqa: BLE001 - a body that cannot be read is not a reason to fail differently
        return ''
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8', errors='replace')
    return re.sub(r'\s+', ' ', str(payload)).strip()[:240]


def fatal_channel_failure(exc) -> str | None:
    """The reason *exc* makes its channel unusable, or ``None`` if it is transient.

    Two shapes are recognised because the two dead channels report differently:
    a raw ``HTTPError`` (Tavily) carries the status on the exception, while the
    model client wraps its status into the message (``DeepSeek 服务返回 HTTP
    400：…``).
    """
    code = getattr(exc, 'code', None)
    if isinstance(code, int) and code in CHANNEL_FATAL_HTTP_STATUSES:
        body = _error_body(exc)
        return f'HTTP {code}' + (f': {body}' if body else '')
    match = _CHANNEL_HTTP_STATUS.search(str(exc))
    if match and int(match.group(1)) in CHANNEL_FATAL_HTTP_STATUSES:
        detail = re.sub(r'\s+', ' ', str(exc)).strip()[:240]
        return detail or f'HTTP {match.group(1)}'
    return None


def impersonation_available() -> bool:
    """Whether the optional TLS-impersonating transport should be used.

    ``curl_cffi`` is not a dependency of this project: the deploy path must work
    without it, so both the package and an explicit switch have to be present.
    A WAF can fingerprint Python's OpenSSL handshake before it ever reads the
    request, which is why the flag is about the *transport*, not the URL.
    """
    if os.getenv('LAB_IMPERSONATE_HTTP') != '1':
        return False
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


def _coerce(rows: Iterable[SearchResult | Mapping], provider: str) -> list[SearchResult]:
    out = []
    for row in rows:
        if isinstance(row, SearchResult):
            out.append(SearchResult(row.url, row.title, row.snippet, provider, row.retrieved_at))
        else:
            out.append(SearchResult(str(row["url"]), str(row.get("title", "")), str(row.get("snippet", row.get("content", ""))), provider))
    return out


@dataclass
class GroundedSearchProvider:
    """Boundary for model-native grounded search; only cited URLs are accepted."""
    client: Callable[[SearchQuery], Sequence[SearchResult | Mapping]] | None = None
    name: str = "grounded_model"
    purposes: frozenset[str] = frozenset({"general", "discovery", "price", "official"})

    def supports(self, query: SearchQuery) -> bool: return query.purpose in self.purposes
    def search(self, query: SearchQuery) -> list[SearchResult]:
        if self.client is None:
            raise ProviderConfigurationError("grounded search client is not configured")
        return _coerce(self.client(query), self.name)[:query.limit]


@dataclass
class DeepSeekGroundedSearchProvider:
    """Model-native web search boundary.

    The provider is enabled only when a runtime DeepSeek key is present. It
    accepts URL citations returned by the model's web tool, while the official
    search orchestrator remains responsible for fetching and classifying the
    cited page body. This keeps model output from becoming product evidence by
    itself.
    """

    client: DeepSeekClient
    name: str = "deepseek_web"
    stop_on_results: bool = True
    purposes: frozenset[str] = frozenset({"general", "discovery", "price", "official", "tender", "procurement", "award"})

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None, transport=None, **kwargs):
        values = os.environ if env is None else env
        if not values.get("DEEPSEEK_API_KEY") or values.get("DEEPSEEK_ENABLE_WEB_SEARCH", "1") != "1":
            return None
        client = DeepSeekClient.from_env(env=values, transport=transport, **kwargs)
        return cls(client)

    def supports(self, query: SearchQuery) -> bool:
        if channel_quarantine_reason(self.name):
            # Out of the chain for the rest of this process: the caller sees a
            # reason, not a silent absence, and no budget unit is spent finding
            # out again what the last task already proved.
            return False
        return query.purpose in self.purposes

    def search(self, query: SearchQuery) -> list[SearchResult]:
        reason = channel_quarantine_reason(self.name)
        if reason:
            raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {reason}')
        try:
            rows = self.client.search_web(query=query.text, purpose=query.purpose, limit=query.limit)
        except DeepSeekError as exc:
            failure = fatal_channel_failure(exc)
            if failure:
                quarantine_channel(self.name, failure)
                raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {failure}') from exc
            raise ProviderConfigurationError(exc.message) from exc
        return _coerce(rows, self.name)[:query.limit]


@dataclass
class ExaMCPSearchProvider:
    """Keyless Exa MCP discovery provider.

    The hosted MCP endpoint is intentionally used as-is; no Exa API key is
    required. Results remain discovery leads and are later fetched/classified
    by ``OfficialSearchService``.
    """

    search_fn: Callable[[SearchQuery, float], Sequence[Mapping]] | None = None
    timeout: float = 12.0
    name: str = "exa_mcp"
    stop_on_results: bool = False
    purposes: frozenset[str] = frozenset({"general", "discovery", "price", "official", "tender", "procurement", "award"})

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        if values.get("EXA_MCP_ENABLED", "1") != "1":
            return None
        try:
            timeout = float(values.get("EXA_MCP_TIMEOUT", "12"))
        except (TypeError, ValueError):
            timeout = 12.0
        return cls(timeout=max(1.0, min(timeout, 30.0)))

    def supports(self, query: SearchQuery) -> bool:
        return query.purpose in self.purposes

    def search(self, query: SearchQuery) -> list[SearchResult]:
        try:
            rows = (self.search_fn or search_exa_results)(query, self.timeout)
        except Exception as exc:
            raise ProviderConfigurationError("Exa MCP unavailable") from exc
        safe_rows = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            url = str(row.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            safe_rows.append({
                "url": url,
                "title": str(row.get("title", ""))[:1000],
                "snippet": str(row.get("snippet", ""))[:2000],
            })
        return _coerce(safe_rows, self.name)[:query.limit]


@dataclass
class TavilySearchProvider:
    """Optional Tavily boundary; key comes from runtime, transport is injected."""
    api_key: str | None = None
    transport: Callable[[str, str, int], Sequence[SearchResult | Mapping]] | None = None
    name: str = "tavily"

    @classmethod
    def from_env(cls, transport=None, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        return cls(api_key=values.get("TAVILY_API_KEY"), transport=transport)

    def supports(self, query: SearchQuery) -> bool:
        return channel_quarantine_reason(self.name) is None

    @staticmethod
    def _default_transport(api_key: str, query: str, limit: int) -> Sequence[Mapping]:
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps({"api_key": api_key, "query": query, "max_results": limit, "search_depth": "advanced"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [
            {"url": item.get("url", ""), "title": item.get("title", ""), "snippet": item.get("content", "")}
            for item in payload.get("results", [])
            if item.get("url")
        ]

    def search(self, query: SearchQuery) -> list[SearchResult]:
        if not self.api_key:
            raise ProviderConfigurationError("TAVILY_API_KEY is not configured")
        reason = channel_quarantine_reason(self.name)
        if reason:
            raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {reason}')
        transport = self.transport or self._default_transport
        try:
            rows = transport(self.api_key, query.text, query.limit)
        except urllib.error.HTTPError as exc:
            failure = fatal_channel_failure(exc)
            # A dead plan is not a transient error: the same 432 came back on
            # every one of this channel's measured calls, so the channel leaves
            # the chain here instead of spending three retries per task.
            if failure:
                quarantine_channel(self.name, failure)
                raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {failure}') from exc
            raise
        return _coerce(rows, self.name)[:query.limit]


@dataclass
class BochaSearchProvider:
    """Optional 博查 (Bocha) web-search boundary; key comes from the runtime.

    Measured 2026-09-30 against the live endpoint: ``POST /v1/web-search`` with
    ``Authorization: Bearer`` and ``{query, count, summary}`` answers HTTP 200
    with ``data.webPages.value[]`` rows (``name``/``url``/``snippet``/``summary``),
    and a deliberately wrong key answers 401 ``Invalid API KEY`` -- so the
    failure is distinguishable from "no results". The shape mirrors
    ``TavilySearchProvider`` on purpose: same injected transport, same
    ``_coerce`` mapping, same quarantine rule for a fatal status.

    One deliberate difference from Tavily: ``from_env()`` returns ``None``
    when the key is absent, so an unconfigured deployment has a byte-identical
    provider chain. Tavily instead always constructs (and fails at search
    time), which only stays harmless because its failure is reported once per
    round; a chain that quietly grows a permanently-failing member is exactly
    what the "same warning count" contract forbids. An explicitly constructed
    ``BochaSearchProvider()`` with no key still raises the ordinary
    ``ProviderConfigurationError``, so the failure vocabulary does not change.
    """

    api_key: str | None = None
    transport: Callable[[str, str, int], Sequence[SearchResult | Mapping]] | None = None
    name: str = "bocha"
    #: Discovery only: it returns URL leads, and the orchestrator still fetches
    #: and classifies the cited body. Never the sole source of a round.
    stop_on_results: bool = False
    timeout: float = 20.0
    purposes: frozenset[str] = frozenset(
        {"general", "discovery", "price", "official", "tender", "procurement", "award"})

    @classmethod
    def from_env(cls, transport=None, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        key = str(values.get("BOCHA_API_KEY") or "").strip()
        if not key:
            # Absent/blank key means "this deployment has no Bocha channel",
            # not "a channel that fails every round".
            return None
        return cls(api_key=key, transport=transport)

    def supports(self, query: SearchQuery) -> bool:
        return channel_quarantine_reason(self.name) is None

    @staticmethod
    def _default_transport(api_key: str, query: str, limit: int) -> Sequence[Mapping]:
        # ``count`` is the site's own result cap; the measured range tops out at
        # 20, so a larger caller limit is clamped rather than sent and refused.
        count = max(1, min(int(limit or 1), 20))
        request = urllib.request.Request(
            "https://api.bochaai.com/v1/web-search",
            data=json.dumps({"query": query, "count": count, "summary": True},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        pages = (((payload.get("data") or {}).get("webPages") or {}).get("value") or [])
        return [
            {"url": item.get("url", ""),
             "title": item.get("name", ""),
             "snippet": item.get("snippet") or item.get("summary") or ""}
            for item in pages
            if item.get("url")
        ]

    def search(self, query: SearchQuery) -> list[SearchResult]:
        if not self.api_key:
            raise ProviderConfigurationError("BOCHA_API_KEY is not configured")
        reason = channel_quarantine_reason(self.name)
        if reason:
            raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {reason}')
        transport = self.transport or self._default_transport
        try:
            rows = transport(self.api_key, query.text, query.limit)
        except urllib.error.HTTPError as exc:
            failure = fatal_channel_failure(exc)
            # 401 ``Invalid API KEY`` is as final as Tavily's 432: the same
            # answer came back on the measured call, so the channel leaves the
            # chain here instead of spending a call per round proving it again.
            if failure:
                quarantine_channel(self.name, failure)
                raise ProviderConfigurationError(f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {failure}') from exc
            raise
        return _coerce(rows, self.name)[:query.limit]


#: A PDF can draw the same glyph twice, offset by a fraction of a point, to fake
#: a bold face. pdfplumber reports every drawing, so a heading reads 品品目目
#: where the page shows 品目. PyMuPDF's text pass, pdfium and docling's layout
#: pass all merge the twin; the table extractor has to ask for it. On a real
#: university award notice the second copy sits 0.42pt left of the first. Left
#: alone, every keyword in an overprinted heading doubles, so a
#: price row's label no longer matches anything the readers look for.
OVERPRINT_TOLERANCE = 1.0


def collapse_overprinted_chars(page):
    """Return *page* with twin-drawn glyphs collapsed to one.

    ``Page.dedupe_chars`` re-sorts and re-clusters every character on the page,
    so it is only worth its cost where an overprint actually exists: a page
    whose characters are all distinct in text, face and position comes back
    untouched.
    """
    seen = set()
    for char in page.chars:
        key = (char.get('text'), char.get('fontname'),
               round(float(char.get('size') or 0), 1),
               round(float(char.get('doctop') or 0)), round(float(char.get('x0') or 0)))
        if key in seen:
            return page.dedupe_chars(tolerance=OVERPRINT_TOLERANCE)
        seen.add(key)
    return page


#: A publisher's ``报价明细附件.zip`` is one archive of line items, not a
#: filesystem image. Every limit below is a refusal, not a truncation: a file we
#: cannot read whole must stay visibly unread rather than silently partial.
#: The size check reads the member stream and counts what actually came out, so
#: a forged header declaring 1 KB cannot smuggle a decompression bomb past it.
ARCHIVE_ENTRY_LIMIT = 32
ARCHIVE_ENTRY_BYTES = 32 * 1024 * 1024
ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
#: Members worth reading. ``.xls``/``.doc`` are the pre-2007 OLE containers the
#: decoder already refuses explicitly; they are listed so the refusal names the
#: file instead of reporting the whole archive as empty.
_READABLE_MEMBER = re.compile(r'\.(?:xlsx|xlsm|csv|txt|pdf|docx)\s*$', re.I)
_LEGACY_MEMBER = re.compile(r'\.(?:xls|doc)\s*$', re.I)


def _member_names(path_name: str) -> bool:
    """Whether a zip member name stays inside the archive.

    Nothing here is ever written to disk, but a name that escapes its own root
    is attacker-controlled input either way: it would decide the label a price
    row is cited under, and the same check is what a future extract-to-disk
    would need. Backslashes are separators on the platform this runs on, so
    ``..\\x.xlsx`` has to fail the same way ``../x.xlsx`` does.
    """
    name = (path_name or '').replace('\\', '/').strip()
    if not name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        return False
    return '..' not in name.split('/')


#: CCGP's attachment-download host. On an announcement page those anchors are
#: ``href=''`` plus ``id=<uuid>``, and the real address is assembled by page
#: script into ``http://download.ccgp.gov.cn/oss/download?uuid=<id>`` -- so the
#: body reader reaches this host straight from the search result, not from the
#: notice the attachment belongs to.
CCGP_DOWNLOAD_HOST = "download.ccgp.gov.cn"
#: That host refuses a request whose ``Referer`` is empty. Measured on the seven
#: 403s of : with no ``Referer`` the same URL answered 403 (edge
#: ``Server: nginx``) on one attempt and 200 on the next,
#: while every attempt carrying a ccgp.gov.cn ``Referer`` answered 200 with
#: ``Server: openresty`` and the real ``Content-Disposition``. A notice URL is
#: preferred when the caller knows it; this public homepage is the fallback for
#: a record whose parent page is unknown. It is a real page of the same site,
#: not a forged identity.
CCGP_DOWNLOAD_REFERER = "http://www.ccgp.gov.cn/"

#: CCGP 这一族主机**不**走可选 TLS 模拟传输，走 stdlib（短连接）。
#:
#: 实测（2026-09-26 凌晨，本机，串行无并发，每次间隔 >=7 秒，客户端就是本模块
#: 的读取器）：同一分钟里 ``www.ccgp.gov.cn`` 的一篇成交公告，
#: * stdlib 分支 -> **200**，正文里型号与单价都在；
#: * 模拟分支 -> **403**；给模拟分支显式加 ``Connection: close`` **仍然是 403**；
#: * 同一分钟用 curl 带上产品自己那四个头（UA／完整 Accept／Accept-Language／Referer）
#:   -> **200**，同一个 75649 字节。
#: 也就是说正文这一格的成败**不是**连接姿态（模拟传输发的本来也不是 keep-alive，
#: 而是 ``Connection: Upgrade, HTTP2-Settings`` + 一套 Chrome 客户端提示），
#: 而是模拟传输在这个站上的整体姿态；换回 stdlib 就通了。
#:
#: 检索主机 ``search.ccgp.gov.cn`` 两条路都能拿到真结果页（stdlib 实测 1 条命中、
#: 模拟实测 25599 字节真结果页），所以这条例外在检索那一格不损失任何东西。
#: 后缀覆盖本模块早就当作"一个站"处理的整族（search／正文／附件三个主机），
#: 它是**主机清单**而不是开关——依赖 TLS 指纹绕 WAF 的其它站一个都不受影响。
CCGP_TRANSPORT_EXEMPT_SUFFIX = "ccgp.gov.cn"


def ccgp_exempt_from_impersonation(url: str) -> bool:
    """Whether *url* is a CCGP host that must use the stdlib transport.

    See :data:`CCGP_TRANSPORT_EXEMPT_SUFFIX` for the measurement behind the
    rule. The test is a hostname suffix, so no path or query string can talk
    its way past it, and a lookalike such as ``ccgp.gov.cn.attacker.example``
    is not exempt.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    return host.endswith(CCGP_TRANSPORT_EXEMPT_SUFFIX)

#: 站点自己宣告的 TLS 姿态里，产品认得的那几种。
#:
#: ``legacy-renegotiation`` 不是"绕过访问控制"，是**这个站点只接受这种握手**：
#: ``www.jxemall.com``（江西省政府采购电子卖场）的公告接口在 OpenSSL 3.x 下会直接
#: 握手失败——实测（2026-09-25，默认 ``ssl.create_default_context``、无凭据、
#: 只读一个公开接口）：
#:
#: * ``[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled``
#:   ——JSON body 与表单编码两种写法都是这一条（``_probe/p7_jiangxi_tls.py``）；
#: * 加上 ``OP_LEGACY_SERVER_CONNECT``、清掉 ``OP_NO_RENEGOTIATION`` 之后，同一个
#:   公开接口正常返回（/也是这么记的）。
#:
#: 所以这是**站点的握手姿态**，由平台配置声明、只对这个请求生效；请求本身一条守卫
#: 都没少（``network_url`` 公网地址校验、重定向逐跳校验、16MB 上限），也不带任何凭据。
#: 改动面有界：给读取器**多发一个参数**，没有这个参数时所有既有读取走的还是默认上下文。
TLS_PROFILES = frozenset({'legacy-renegotiation'})


def _tls_handlers(tls_profile: str | None) -> tuple:
    """按站点宣告的 TLS 姿态给出额外的手 opener handler；默认姿态返回空。

    只认 :data:`TLS_PROFILES` 里登记的姿态；写错的名字直接报错，不做静默降级——
    "以为是那个姿态、其实用了默认上下文"会让失败看起来像站点坏了。
    """
    if tls_profile is None:
        return ()
    if tls_profile not in TLS_PROFILES:
        raise ValueError('未知的 TLS 姿态: ' + str(tls_profile))
    import ssl
    context = ssl.create_default_context()
    if tls_profile == 'legacy-renegotiation':
        context.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)
        context.options &= ~getattr(ssl, 'OP_NO_RENEGOTIATION', 0)
    return (urllib.request.HTTPSHandler(context=context),)


@dataclass
class PublicWebSearchProvider:
    """Keyless public-web fallback used when grounded/Tavily is unavailable.

    Bing's public HTML result page is used only to discover URLs and snippets;
    ``OfficialSearchService`` still fetches each result body before it can be
    treated as evidence. The transport is injectable so tests never call the
    network, while production keeps a short timeout and a normal user agent.
    """

    transport: Callable[[str, float], tuple[bytes, str | None]] | None = None
    name: str = "public_web"
    endpoint: str = "https://www.bing.com/search"
    timeout: float = 8.0
    #: ``Referer`` to send with this view's requests. ``None`` means "decide from
    #: the URL" (see ``_request_headers``). Set by ``for_referer`` so one read
    #: can carry the page it was linked from without mutating a shared reader.
    referer: str | None = None

    _TENDER_MARKERS = (
        "招标", "中标", "成交", "采购结果", "采购公告", "公告", "评标", "开标",
        "tender", "award", "procurement", "contract award", "bid notice",
    )

    @classmethod
    def _is_tender_intent(cls, query: SearchQuery) -> bool:
        text = query.text.casefold()
        return query.purpose.casefold() in {"price", "tender", "procurement", "award"} or any(
            marker.casefold() in text for marker in cls._TENDER_MARKERS
        )

    @classmethod
    def _tender_queries(cls, query: SearchQuery) -> list[str]:
        """Turn a vague price query into discovery queries for formal results.

        This affects only the keyless fallback and does not turn snippets into
        evidence. The official-search orchestrator still fetches and validates
        the discovered document body before assigning a historical price tier.
        """
        text = re.sub(r"[\"'“”]", " ", query.text)
        text = re.sub(
            r"\b(?:official|specifications?|price|tender|award|procurement|notice|pdf|filetype:pdf)\b",
            " ", text, flags=re.I,
        )
        for marker in cls._TENDER_MARKERS:
            text = text.replace(marker, " ")
        base = re.sub(r"\s+", " ", text).strip()
        if not base:
            base = query.text.strip()
        if re.search(r"[\u3400-\u9fff]", base):
            return [
                f'"{base}" 中标 成交 采购结果 filetype:pdf',
                f'"{base}" 招标公告 采购公告',
            ]
        return [
            f'"{base}" tender award procurement filetype:pdf',
            f'"{base}" tender award notice',
        ]

    @classmethod
    def _relevance_score(cls, row: Mapping[str, str]) -> int:
        haystack = f"{row.get('title', '')} {row.get('snippet', '')} {row.get('url', '')}".casefold()
        score = sum(marker.casefold() in haystack for marker in cls._TENDER_MARKERS)
        if "filetype:pdf" in haystack or row.get("url", "").casefold().endswith(".pdf"):
            score += 1
        host = (urllib.parse.urlsplit(row.get("url", "")).hostname or "").casefold()
        if host.endswith((".gov.cn", ".gov", ".edu.cn")):
            score += 1
        return score

    def supports(self, query: SearchQuery) -> bool:
        return True

    def _request(self, url: str, timeout: float, *, data: bytes | None = None,
                 extra_headers: Mapping[str, str] | None = None,
                 method: str | None = None,
                 tls_profile: str | None = None) -> tuple[bytes, str | None]:
        if self.transport is not None:
            # An injected transport answers a POST the same way it answers a GET:
            # the platform module is not allowed a second HTTP client, so its
            # form postbacks have to arrive here too. Tests that only care about
            # *which* URLs a run touches stay valid; a transport used to fake a
            # body still simply returns it.
            return self.transport(url, timeout)
        from backend.services.research_sources import network_url
        network_url(url)
        parsed = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.encode('idna').decode('ascii'),
                                      urllib.parse.quote(parsed.path, safe='/%:@!$&\'()*+,;=-._~'),
                                      urllib.parse.quote(parsed.query, safe='%=&/?:@!$\'()*+,;~-._'), ''))
        class PublicRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                network_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        # The optional transport answers the same URL with a browser-shaped TLS
        # handshake; the guards below (validated hops, payload cap) are the same
        # because they constrain *what* may be fetched, not which library does it.
        # The impersonated path only speaks GET, so a form postback takes the
        # stdlib path -- with the same hop validation and payload cap.
        # CCGP is the one host family excluded on purpose: the measurement in
        # ``CCGP_TRANSPORT_EXEMPT_SUFFIX`` shows the impersonated client is
        # answered 403 there where the stdlib client reads the same page. Every
        # other host, including the WAF sites the TLS profile exists for, keeps
        # the old branch.
        if (impersonation_available() and data is None
                and not ccgp_exempt_from_impersonation(url)):
            return self._impersonated_request(url, timeout)
        headers = {**self._request_headers(url), **(extra_headers or {})}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # Some university publishers set a session cookie before redirecting
        # to the same public document. Keep it only for this bounded request.
        import http.cookiejar
        opener = urllib.request.build_opener(*_tls_handlers(tls_profile), PublicRedirect(),
                                             urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise ValueError('附件超过16MB，请提供具体明细页')
            return payload, response.headers.get("Content-Type")

    def fetch_form(self, url: str, *, fields: Mapping[str, str] | None = None,
                   body: bytes | str | None = None, content_type: str | None = None,
                   tls_profile: str | None = None,
                   method: str = 'POST', extra_headers: Mapping[str, str] | None = None,
                   timeout: float = 20.0) -> tuple[bytes, str | None]:
        """One form submission to a public page, through the guarded GET path.

        The 教育／科研交易平台 lists are ASP.NET WebForms and municipal JSON
        endpoints: the only way to filter or page them is a POST, so they cannot
        be read with ``fetch_raw``. Rather than let the platform module open its
        own connection -- which would bypass ``network_url``, the redirect-hop
        validation, the payload cap and any transport a test injected -- the
        postback is handed to ``_request``. The platform module therefore stays
        a *parser*: it describes the form, this layer performs the read.

        ``body`` 与 ``fields`` 二选一：省级政采平台的查询接口吃的是
        ``application/json``，而 ``fields`` 只会被 ``urlencode`` 成
        ``application/x-www-form-urlencoded``。给 ``body`` 时把 ``content_type``
        一起说清楚，本层仍旧只做"发出去"这件事，守卫一条没少。
        ``tls_profile`` 是站点自己宣告的握手姿态（见 :data:`TLS_PROFILES`），
        默认 ``None`` = 现代握手。
        """
        if body is not None and fields:
            raise ValueError('body 与 fields 只能给一个')
        headers = dict(extra_headers or {})
        if body is not None:
            data = body if isinstance(body, bytes) else str(body).encode('utf-8')
            if content_type:
                headers.setdefault('Content-Type', content_type)
        else:
            data = urllib.parse.urlencode(fields).encode('utf-8') if fields else None
        if data is None:
            # No body means this is a plain list read; asking for POST would make
            # urllib send a zero-length body and some sites answer that with 411.
            method = 'GET'
        elif body is None:
            headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        return self._request(url, timeout, data=data, extra_headers=headers,
                             method=method, tls_profile=tls_profile)

    def _impersonated_request(self, url: str, timeout: float) -> tuple[bytes, str | None]:
        """One TLS-impersonated fetch that keeps the stdlib path's guarantees.

        Redirects are followed by hand so every hop is re-validated with
        ``network_url`` -- the stdlib path gets that from ``PublicRedirect`` --
        and a non-2xx status is raised as ``HTTPError`` so the failure
        vocabulary the rest of the chain relies on does not change with the
        transport.
        """
        from urllib.error import HTTPError
        from curl_cffi import requests as curl_requests
        from backend.services.research_sources import network_url
        with curl_requests.Session(impersonate='chrome') as session:
            for _hop in range(6):
                response = session.get(url, timeout=timeout, allow_redirects=False,
                                       headers=self._request_headers(url))
                location = response.headers.get('Location')
                if response.status_code in {301, 302, 303, 307, 308} and location:
                    # ``Location`` 是对面服务器给的任意字符串：解析不了就是"这个跳转
                    # 没法跟"，不该让它变成一个读不出来的异常类型（统一入口见
                    # ``research_sources.parse_web_url``）。
                    from backend.services.research_sources import parse_web_url
                    url = parse_web_url(location, url)
                    if url is None:
                        raise ValueError('重定向地址无法解析')
                    network_url(url)
                    continue
                if response.status_code >= 400:
                    raise HTTPError(url, response.status_code, str(response.reason), response.headers, None)
                payload = response.content
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError('附件超过16MB，请提供具体明细页')
                return payload, response.headers.get('Content-Type')
        raise ValueError('重定向次数过多')

    def _request_headers(self, url: str) -> dict[str, str]:
        """Default public request profile; subclasses may override.

        Institutional publishers and procurement portals routinely block
        self-identifying tool agents at the WAF, so public reads present a
        normal browser profile.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        referer = self.referer or (
            CCGP_DOWNLOAD_REFERER
            if (urllib.parse.urlsplit(url).hostname or "").casefold() == CCGP_DOWNLOAD_HOST
            else None)
        if referer:
            headers["Referer"] = referer
        return headers

    def for_referer(self, referer: str | None):
        """A view of this provider that sends *referer* with its requests.

        ``None`` or an empty value returns ``self`` unchanged, so a caller can
        pass ``source.get('parent_url')`` unconditionally. A new object is
        returned rather than the attribute being set, because a reader instance
        is shared by every read in a run (and by parallel prefetches).
        """
        value = (referer or "").strip() or None
        if value is None or value == self.referer:
            return self
        return replace(self, referer=value)

    @staticmethod
    def _clean_html(value: str, base_url: str = '') -> str:
        value = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", value, flags=re.I)
        # Institutional pages often name a manufacturer in an anchor while
        # the domain appears only in href. Preserve that evidence association.
        def retain_link(match):
            href, label = unescape(match.group(1)), match.group(2)
            # ``href`` 是别人页面上的任意字符串：主机名尾巴带全角标点的那种畸形
            # 链接会让 ``urljoin`` 抛 ``ValueError``（标准库 ``_checknetloc``），
            # 而这一处跑在 ``re.sub`` 的回调里——一个坏 href 就能让**整页正文
            # 清洗失败**。读不出来的地址按"没有地址"处理，保留锚文本。
            from backend.services.research_sources import parse_web_url
            href = parse_web_url(href, base_url)
            if href is None or urllib.parse.urlsplit(href).scheme.lower() not in {'http', 'https'}:
                return label
            return label + ' [' + href + '] '
        value = re.sub(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', retain_link, value, flags=re.I)
        from backend.services.web_tables import preserve_tables
        value = preserve_tables(value)
        # Paragraphs inside individual table cells are not row boundaries.
        value = re.sub(r'(<tr\b[^>]*>)([\s\S]*?)(</tr\s*>)',
                       lambda m: m.group(1) + re.sub(r'</(?:p|div)\s*>|<br\s*/?>', ' ', m.group(2), flags=re.I) + m.group(3), value, flags=re.I)
        value = re.sub(r'</(?:tr|p|div|li|h[1-6])\s*>|<br\s*/?>', '\n', value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        return '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in unescape(value).splitlines() if line.strip())

    @classmethod
    def _parse_results(cls, payload: bytes, limit: int) -> list[Mapping[str, str]]:
        import base64
        from backend.infrastructure.search_markup import result_rows
        text = payload.decode("utf-8", errors="replace")
        rows = []
        for row in result_rows(text, 'bing'):
            url = row['url'].strip()
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname in {'www.bing.com', 'cn.bing.com', 'bing.com'} and parsed.path == '/ck/a':
                encoded = urllib.parse.parse_qs(parsed.query).get('u', [''])[0]
                if not encoded.startswith('a1'):
                    continue
                try:
                    value = encoded[2:]
                    url = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode('utf-8')
                except (ValueError, UnicodeError):
                    continue
            if not url.startswith(("http://", "https://")):
                continue
            rows.append({**row, 'url': url})
            if len(rows) >= limit:
                break
        return rows

    def search(self, query: SearchQuery) -> list[SearchResult]:
        texts = self._tender_queries(query) if self._is_tender_intent(query) else [query.text]
        rows: list[Mapping[str, str]] = []
        seen: set[str] = set()
        for text in texts:
            url = f"{self.endpoint}?{urllib.parse.urlencode({'q': text, 'count': query.limit})}"
            payload, _content_type = self._request(url, self.timeout)
            for row in self._parse_results(payload, query.limit):
                key = row["url"].strip().casefold()
                if key in seen:
                    continue
                seen.add(key)
                if self._is_tender_intent(query) and self._relevance_score(row) < 1:
                    continue
                rows.append(row)
                if len(rows) >= query.limit:
                    break
            if len(rows) >= query.limit:
                break
        return _coerce(rows[:query.limit], self.name)

    def search_once(self, query: SearchQuery) -> list[SearchResult]:
        """Budgeted research must reserve one call per HTTP search."""
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query.text, 'count': query.limit})}"
        payload, _ = self._request(url, self.timeout)
        return _coerce(self._parse_results(payload, query.limit), self.name)

    def fetch(self, url: str, timeout: float) -> str:
        # An injected transport is authoritative. Besides keeping tests
        # deterministic, this lets callers enforce their own network policy
        # without an implicit request escaping through Jina Reader first.
        if self.transport is not None:
            payload, content_type = self._request(url, timeout)
            return self.decode_body(payload, content_type, url)

        # Try Jina Reader first for higher success rate on complex pages
        if os.environ.get("ENABLE_JINA_READER", "true").lower() == "true":
            try:
                # Validate timeout configuration
                try:
                    jina_timeout = float(os.environ.get("JINA_READER_TIMEOUT", "10"))
                except ValueError:
                    jina_timeout = 10.0  # fallback to default if invalid

                from backend.infrastructure.jina_reader import fetch_with_jina
                # Allocate 70% of timeout budget to Jina to leave room for fallback
                text, _metadata = fetch_with_jina(url, timeout=min(jina_timeout, timeout * 0.7))
                return text
            except Exception as e:
                # Log Jina failure for observability, then fall back to urllib
                import logging
                logging.debug(f"Jina Reader failed for {url}: {type(e).__name__}: {e}")

        # Fallback to direct urllib fetch
        payload, content_type = self._request(url, timeout)
        return self.decode_body(payload, content_type, url)

    @staticmethod
    def _pdf_tables_text(payload: bytes, page_limit: int = 60):
        """Extract PDF text as table-shaped '|' rows plus narrative context.

        Returns ``(text, report)``. The report records what the extractor could
        *reach*: a caller cannot tell a complete document from a scan's page
        furniture by looking at the returned text alone, and a page that holds
        only an image is exactly the page OCR exists for.
        """
        import pdfplumber
        lines: list[str] = []
        pages = pages_with_text = image_only = images = characters = 0
        with pdfplumber.open(BytesIO(payload)) as document:
            if len(document.pages) > page_limit:
                raise ValueError(f'PDF页数超过{page_limit}页，请提供具体明细页')
            pages = len(document.pages)
            for page in document.pages:
                characters += len(page.chars)
                images += len(page.images)
                if page.chars:
                    pages_with_text += 1
                elif page.images:
                    image_only += 1
                page = collapse_overprinted_chars(page)
                tables = page.find_tables()
                bboxes = [table.bbox for table in tables]
                for table in tables:
                    for row in table.extract():
                        cells = [re.sub(r'\s+', ' ', (cell or '')).strip() for cell in row]
                        if any(cells):
                            lines.append(' | '.join(cells))
                if bboxes:
                    # Narrative text outside detected tables keeps context such
                    # as award-decision wording that row extractors also read.
                    def _outside(obj, bboxes=bboxes):
                        return not any(
                            obj.get('x0', 0) >= box[0] and obj.get('x1', 0) <= box[2]
                            and obj.get('top', 0) >= box[1] and obj.get('bottom', 0) <= box[3]
                            for box in bboxes
                        )
                    text = page.filter(_outside).extract_text() or ''
                else:
                    text = page.extract_text() or ''
                lines.extend(line.strip() for line in text.splitlines() if line.strip())
        report = {'pages': pages, 'pages_with_text': pages_with_text,
                  'image_only_pages': image_only, 'images': images, 'characters': characters,
                  # Not a threshold: text extraction demonstrably cannot see a
                  # page that carries an image and no characters.
                  'incomplete': image_only > 0}
        return '\n'.join(lines).strip(), report

    @classmethod
    def _decode_pdf(cls, payload):
        """Return ``(text, report)`` for a PDF payload, or raise."""
        try:
            structured, report = cls._pdf_tables_text(payload)
        except ValueError:
            raise
        except Exception:
            structured, report = '', {}
        if structured:
            return structured, report
        try:
            from pypdf import PdfReader
            pages = PdfReader(BytesIO(payload)).pages
            text = "\n".join(page.extract_text() or "" for page in pages)
            if text.strip():
                return text.strip(), report
        except Exception:
            # Keep the body failure visible to the orchestrator rather than
            # inventing text from a binary PDF payload.
            raise ValueError('PDF正文无法解析，请人工核验附件')
        raise ValueError('PDF没有可读取文本，请补充OCR')

    @classmethod
    def _package_body(cls, payload):
        """Read a ``PK`` payload: an Office document, or an archive of them.

        The Office case is unchanged -- the package *is* the document. Anything
        else is a publisher's archive of files (``报价明细附件.zip``), and that
        archive is a body only if one of its members is readable. Returning ""
        for it would report a file nobody opened as "no line items", which is
        the one thing this lane exists to prevent.

        A docx additionally reports its own structure (``_word_document``), so a
        caller can tell which rows came from a table and which narrative line
        introduced that table.
        """
        from zipfile import BadZipFile, ZipFile
        try:
            archive = ZipFile(BytesIO(payload))
        except BadZipFile as exc:
            raise ValueError('附件不是可读取的压缩包，原件已保留') from exc
        # A truncated download leaves a readable directory whose member data
        # then fails CRC, a corrupt deflate stream raises zlib.error, and an
        # over-long CSV field raises csv.Error. None of those is a ValueError,
        # and the caller above catches only ValueError, so an archive that
        # cannot be read would surface as an unexplained failure instead of a
        # reason -- and would skip the structured-PDF fallback on the way out.
        # Refuse it here, while the cause is still known.
        from csv import Error as CsvError
        from zlib import error as ZlibError
        try:
            with archive:
                names = set(archive.namelist())
                report = {}
                if 'word/document.xml' in names:
                    text, report = cls._word_document(payload)
                elif 'xl/workbook.xml' in names:
                    text = cls._workbook_text(payload)
                else:
                    text = cls._members_text(archive)
        except (BadZipFile, ZlibError, CsvError, NotImplementedError, KeyError) as exc:
            raise ValueError('附件压缩包损坏或格式不受支持，原件已保留：'
                             + type(exc).__name__) from exc
        if not text:
            raise ValueError('附件没有可读取正文')
        return text, report

    @staticmethod
    def _word_document(payload):
        """Read a docx into the row shape PDF/XLSX already produce, plus a map.

        The row shape is deliberate: a table row is its cells joined by `` | ``,
        so every downstream row extractor works on a docx unchanged. A second
        parser for one shape is the cost being avoided here.

        What the row shape cannot carry is *which* rows came from a table and
        which narrative line introduced it. Without that map the 13 requirement
        rows under 「三、血型血清学离心机」 and the rows under 「一、凝血全血凝固
        时间监测仪」 are indistinguishable in the text, so the requirements get
        bound to the wrong instrument -- the same loss the PDF lane avoids by
        keeping narrative text outside the table bounding boxes. The report
        therefore records, per table, the line span it occupies and the nearest
        non-empty line before it.
        """
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with ZipFile(BytesIO(payload)) as archive:
            body = ET.fromstring(archive.read('word/document.xml')).find('w:body', ns)
        lines, tables, paragraphs, heading, heading_line = [], [], 0, '', None
        for block in body:
            if block.tag.endswith('}tbl'):
                table = {'index': len(tables), 'start_line': len(lines), 'heading': heading[:120],
                         'heading_line': heading_line, 'rows': 0}
                for row in block.findall('w:tr', ns):
                    cells = [''.join(cell.itertext()) if not cell.findall('.//w:t', ns)
                             else ''.join(t.text or '' for t in cell.findall('.//w:t', ns))
                             for cell in row.findall('w:tc', ns)]
                    if any(cells):
                        lines.append(' | '.join(cells))
                        table['rows'] += 1
                table['end_line'] = len(lines) - 1
                tables.append(table)
            else:
                text = ''.join(t.text or '' for t in block.findall('.//w:t', ns))
                lines.append(text)
                if text.strip():
                    paragraphs += 1
                    heading, heading_line = text.strip(), len(lines) - 1
        # 行号必须能用返回文本的 split('\n') 直接索引：裁掉的首尾空行会让
        # 「这张表的标题在第几行」整体偏移，绑定于是串到别的设备上（实测：
        # 偏移 3 行时 13 条离心机要求会被算到凝血仪名下）。所以先把行列表
        # 裁到与返回文本一致，再按同一个偏移改写报告里的位置。
        offset = 0
        while offset < len(lines) and not lines[offset].strip():
            offset += 1
        tail = len(lines)
        while tail > offset and not lines[tail - 1].strip():
            tail -= 1
        lines = lines[offset:tail]
        for table in tables:
            for key in ('start_line', 'end_line', 'heading_line'):
                if isinstance(table.get(key), int):
                    table[key] = max(0, table[key] - offset)
        report = {'paragraphs': paragraphs, 'tables': len(tables),
                  'table_rows': sum(table['rows'] for table in tables),
                  'table_index': tables,
                  # Text extraction reaches every character a docx carries; it
                  # cannot see a page rendered as an image, which is the PDF
                  # report's meaning. Kept explicit so callers need no special
                  # case for this document type.
                  'incomplete': False}
        return '\n'.join(lines).strip(), report

    @staticmethod
    def _word_text(payload):
        return PublicWebSearchProvider._word_document(payload)[0]

    @staticmethod
    def _workbook_text(payload):
        import openpyxl
        workbook = openpyxl.load_workbook(BytesIO(payload), read_only=True, data_only=True)
        try:
            lines = []
            count = 0
            for sheet in workbook:
                lines.append('工作表：' + sheet.title)
                for row in sheet.iter_rows(values_only=True):
                    count += len(row)
                    if count > 200000:
                        raise ValueError('表格过大，请提供具体采购明细')
                    if any(v is not None for v in row):
                        lines.append(' | '.join('' if v is None else str(v) for v in row))
            return '\n'.join(lines).strip()
        finally:
            workbook.close()

    @staticmethod
    def _csv_text(data):
        """Normalise a csv member to the pipe rows every row reader expects.

        ``price_table_rows`` splits on ``|`` -- the shape XLSX and PDF decoding
        already produce -- so a comma-separated price list has to be converted
        here rather than handed over raw, where no header would ever match.
        """
        import csv
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            rows = csv.reader(StringIO(text))
            lines = [' | '.join(cell.strip() for cell in row) for row in rows]
            return '\n'.join(line for line in lines if line.strip(' |'))
        raise ValueError('CSV编码无法可靠读取')

    @staticmethod
    def _read_member(archive, info):
        """Read one member under a hard cap on what decompression may produce.

        The count is of bytes that came out, not of the size the header claims:
        a bomb declares a small ``file_size`` and then inflates.
        """
        with archive.open(info) as handle:
            data = handle.read(ARCHIVE_ENTRY_BYTES + 1)
        if len(data) > ARCHIVE_ENTRY_BYTES:
            raise ValueError('附件压缩包内单个文件解压后过大，已拒绝整包读取')
        return data

    @classmethod
    def _members_text(cls, archive):
        """Flatten a publisher's archive into one body, or refuse it whole."""
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > ARCHIVE_ENTRY_LIMIT:
            raise ValueError(f'附件压缩包含{len(infos)}个文件，超过{ARCHIVE_ENTRY_LIMIT}个，已拒绝整包读取')
        for info in infos:
            if not _member_names(info.filename):
                raise ValueError('附件压缩包内含越界路径，已拒绝整包读取：' + info.filename[:80])
        if sum(getattr(info, 'file_size', 0) or 0 for info in infos) > ARCHIVE_TOTAL_BYTES:
            raise ValueError('附件解压后过大，请提供具体明细页')
        lines, skipped = [], []
        for info in infos:
            name = info.filename
            if _LEGACY_MEMBER.search(name):
                skipped.append(name + '：旧版Word/Excel二进制附件暂不可读取，请提供DOCX/XLSX或PDF')
                continue
            if not _READABLE_MEMBER.search(name):
                skipped.append(name + '：不是可读取的明细格式')
                continue
            data = cls._read_member(archive, info)
            try:
                text = cls._member_text(name, data)
            except ValueError as exc:
                skipped.append(name + '：' + str(exc))
                continue
            if text.strip():
                # The member's own name is kept: a citation that cannot say
                # which file inside the archive it came from is not auditable.
                lines.append('【附件包内文件】' + name)
                lines.append(text.strip())
            else:
                skipped.append(name + '：没有可读取正文')
        if not lines:
            detail = '；'.join(skipped[:5])
            raise ValueError('压缩包内没有可读取的明细文件' + ('（' + detail + '）' if detail else ''))
        return '\n'.join(lines)

    @classmethod
    def _member_text(cls, name, data):
        lowered = name.lower()
        if lowered.endswith('.pdf'):
            return cls._decode_pdf(data)[0]
        if lowered.endswith('.csv'):
            return cls._csv_text(data)
        if lowered.endswith(('.xlsx', '.xlsm')):
            return cls._workbook_text(data)
        if lowered.endswith('.docx'):
            return cls._word_text(data)
        return cls.decode_body_with_report(data, 'text/plain', name)[0]

    @classmethod
    def decode_body(cls, payload, content_type, url=''):
        return cls.decode_body_with_report(payload, content_type, url)[0]

    @classmethod
    def decode_body_with_report(cls, payload, content_type, url=''):
        """Decode *payload* and report how much of the document was reached.

        "The parser raised" and "we obtained the document" are different
        questions. A scanned attachment returns a little page furniture and no
        exception, so a non-empty result is not evidence of completeness; the
        report is what lets a caller escalate to the structured parser instead
        of reporting an image-only price table as successfully read.
        """
        if payload.startswith(b'\x1f\x8b'):
            import gzip
            with gzip.GzipFile(fileobj=BytesIO(payload)) as compressed:
                payload = compressed.read(64 * 1024 * 1024 + 1)
            if len(payload) > 64 * 1024 * 1024:
                raise ValueError('压缩正文解压后过大')
        if payload.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            raise ValueError('旧版Word/Excel二进制附件暂不可读取，原件已保留，请提供DOCX/XLSX或PDF')
        # Office XML packages can carry procurement line items without HTML,
        # and a publisher's .zip carries several of them at once.
        if payload.startswith(b'PK'):
            return cls._package_body(payload)
        if payload.startswith(b"%PDF") or (content_type and "pdf" in content_type.lower()):
            # Award line items live in tables; structured rows keep the '|'
            # cell shape that HTML/XLSX decoding already produces, so the same
            # downstream row extractors work on PDF attachments.
            return cls._decode_pdf(payload)
        if content_type and "text" not in content_type.lower() and "html" not in content_type.lower():
            raise ValueError('暂不支持此附件类型')
        if b'\x00' in payload[:4096]:
            raise ValueError('二进制附件不能作为网页正文')
        charset = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type or '', re.I)
        if not charset:
            charset = re.search(r'charset\s*=\s*["\']?([\w-]+)', payload[:4096].decode('ascii', errors='ignore'), re.I)
        encoding = charset.group(1) if charset else 'utf-8'
        try:
            text = payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            try:
                text = payload.decode('gb18030')
            except UnicodeDecodeError as exc:
                raise ValueError('网页编码无法可靠读取') from exc
        return cls._clean_html(text, url), {}

    def fetch_snapshot(self, url: str, timeout: float):
        payload, content_type = self.fetch_raw(url, timeout)
        return payload, content_type, self.decode_body(payload, content_type, url)

    def fetch_raw(self, url: str, timeout: float):
        return self._request(url, timeout)


@dataclass
class SogouSearchProvider(PublicWebSearchProvider):
    """Chinese-first public discovery provider.

    Sogou result links are redirect wrappers. We resolve those wrappers before
    returning a result so the source layer sees the original public URL. The
    result page remains a discovery surface only; body fetching and evidence
    classification still happen in ``OfficialSearchService``.
    """

    name: str = "sogou"
    endpoint: str = "https://www.sogou.com/web"
    resolves_result_links = True

    @staticmethod
    def engine_query(text):
        # This engine treats -"brand" as a positive term in observed results.
        # Enforce brand exclusion downstream, without feeding that false hint.
        return re.sub(r'\s+-"[^"]+"', '', text).strip()

    def search_once(self, query, on_resolve=None):
        url = self.endpoint + '?' + urllib.parse.urlencode({'query': self.engine_query(query.text)})
        payload, _ = self._request(url, self.timeout)
        rows = []
        for raw in self._parse_sogou_results(payload, query.limit):
            resolved = self._resolve_result(raw, on_resolve=on_resolve)
            if resolved:
                rows.append(resolved)
        return _coerce(rows, self.name)

    @classmethod
    def _parse_sogou_results(cls, payload: bytes, limit: int) -> list[Mapping[str, str]]:
        from backend.infrastructure.search_markup import result_rows
        cls._check_challenge(payload)
        text = payload.decode("utf-8", errors="replace")
        rows = []
        for row in result_rows(text, 'sogou'):
            if row['url'] and row['title'] and row not in rows:
                rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _check_challenge(payload):
        text = payload.decode('utf-8', errors='replace')
        if 'SourceVerifyCode' in text or '此验证码用于确认这些请求' in text or re.search(r'<title[^>]*>[^<]*(?:验证码|访问验证)', text, re.I):
            raise ProviderConfigurationError('Sogou returned an access challenge')

    @staticmethod
    def _resolve_redirect(payload: bytes) -> str | None:
        text = payload.decode("utf-8", errors="replace")
        patterns = (
            r'window\.location\.replace\(["\']([^"\']+)["\']\)',
            r'URL=["\']?([^"\' >]+)',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                target = unescape(match.group(1)).strip()
                if target.startswith(("http://", "https://")):
                    return target
        return None

    def _resolve_result(self, row: Mapping[str, str], on_resolve=None) -> Mapping[str, str] | None:
        # 结果是别家页面上的 ``href``：畸形串在这里只能算"这一条结果用不了"，
        # 不能让整个搜索通道抛错（见 ``parse_web_url``）。
        from backend.services.research_sources import parse_web_url
        href = row["url"]
        base = '' if href.startswith(("http://", "https://")) else self.endpoint
        href = parse_web_url(href, base)
        if href is None:
            return None
        if urllib.parse.urlsplit(href).hostname not in {"www.sogou.com", "sogou.com"}:
            return {**row, "url": href}
        if on_resolve:
            on_resolve()
        payload, _content_type = self._request(href, self.timeout)
        self._check_challenge(payload)
        target = self._resolve_redirect(payload)
        return {**row, "url": target} if target else None

    def search(self, query: SearchQuery) -> list[SearchResult]:
        texts = [query.text]
        if self._is_tender_intent(query):
            texts.extend(self._tender_queries(query))
        rows: list[Mapping[str, str]] = []
        seen: set[str] = set()
        for text in texts:
            url = f"{self.endpoint}?{urllib.parse.urlencode({'query': text})}"
            payload, _content_type = self._request(url, self.timeout)
            for raw in self._parse_sogou_results(payload, query.limit):
                row = self._resolve_result(raw)
                if not row:
                    continue
                key = row["url"].strip().casefold()
                if key in seen:
                    continue
                if self._is_tender_intent(query) and self._relevance_score(row) < 1:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) >= query.limit:
                    break
            if len(rows) >= query.limit:
                break
        return _coerce(rows[:query.limit], self.name)


@dataclass
class BaiduSearchProvider(PublicWebSearchProvider):
    """Keyless Chinese discovery channel used to *locate* official notices.

    The aggregator leads carry a brand, a model, a supplier and a category, and
    no original source -- not publishing the source is the sites' own design
    . Those fields are the search keys: this adapter takes one
    keyword combination, returns the result list with real URLs, and the upper
    layer keeps the ``.gov.cn`` / ``.edu.cn`` hits. It is a retrieval channel,
    not a new source -- no per-site adaptation lives here, and no aggregator
    host is special-cased.

    The URL of a result comes from the SERP's own ``mu`` attribute, not from the
    anchor's ``href``. Measured on a live SERP (2026-09-24, ``wd=库车市农业农
    村局 知楚 生物安全柜 中标``): 19 organic cards, every one carrying a ``mu``
    with the target URL, while every anchor pointed at
    ``http://www.baidu.com/link?url=...`` -- a wrapper that costs one extra
    request per row to resolve. Reading ``mu`` keeps one request per keyword and
    hands the ``.gov.cn`` filter already-resolved URLs.
    ``resolves_result_links`` stays False on purpose: to the run's budget that
    flag means "each result needs a follow-up fetch", and this channel does not.

    One page per keyword, like ``SogouSearchProvider``: ``rn=20`` is the page
    size the parse was measured against, and the parsed rows are capped by
    ``query.limit``.
    """

    name: str = "baidu"
    endpoint: str = "https://www.baidu.com/s"
    resolves_result_links = False
    #: Values Baidu serves instead of a result list when it wants a human.
    _CHALLENGE_MARKERS = ("百度安全验证", "wappass.baidu.com")
    #: The headers a real Chrome navigation to ``www.baidu.com/s`` carries on top
    #: of the base profile.  recipe P3 measured exactly these
    #: three on a 1,024,249 B result page. ``Accept`` is overridden rather than
    #: added because the base value is the generic fetch profile, and
    #: ``Referer`` / ``Upgrade-Insecure-Requests`` are absent from it entirely.
    #: These headers are *not* what decides whether the challenge page comes
    #: back -- frequency is. Re-measured 2026-09-26 with this provider: the bare
    #: base profile read a real 10-row result page after the IP had been quiet
    #: for hours, and the same run with these headers was challenged on its
    #: ninth request (,
    #: ``baidu-after-fix.json``). They are kept because they make the request
    #: look like the navigation it is imitating, at no measured cost.
    _BROWSER_HEADERS = {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Referer": "https://www.baidu.com/",
        "Upgrade-Insecure-Requests": "1",
    }

    def _request_headers(self, url: str) -> dict[str, str]:
        """The base browser profile plus this channel's three measured headers.

        Only Baidu gets them. The base profile is shared with Bing, Sogou and
        every public body read, and none of those was measured to behave
        differently with a Baidu referer attached -- so the change stays inside
        this one subclass instead of touching the shared ``_request`` path.
        """
        return {**super()._request_headers(url), **self._BROWSER_HEADERS}

    @staticmethod
    def engine_query(text):
        """Send the keywords as written.

        ``SogouSearchProvider.engine_query`` strips ``-"word"`` because that
        engine reads it as a positive term. Baidu honors it as an exclusion, so
        stripping it here would silently change the caller's query.
        """
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @classmethod
    def _check_challenge(cls, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        if any(marker in text for marker in cls._CHALLENGE_MARKERS) or re.search(
                r"<title[^>]*>[^<]*(?:安全验证|访问验证)[^<]*</title>", text, re.I):
            raise ProviderConfigurationError("Baidu returned an access challenge")

    @classmethod
    def _parse_baidu_results(cls, payload: bytes, limit: int) -> list[Mapping[str, str]]:
        """Organic results of one SERP page, keyed by their real URL."""
        from backend.infrastructure.search_markup import ResultTree
        cls._check_challenge(payload)
        text = payload.decode("utf-8", errors="replace")
        rows: list[Mapping[str, str]] = []
        seen: set[str] = set()
        for block in ResultTree(text).root.walk():
            if block.tag != "div" or not block.classes.issuperset({"c-container", "result"}):
                continue
            url = (block.attrs.get("mu") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            # Baidu's own cards -- top lists, recommendations, placeholders --
            # reuse the result container and carry a mu of their own.
            if not host or host == "baidu.com" or host.endswith(".baidu.com"):
                continue
            heading = next((n for n in block.walk() if n.tag == "h3"), None)
            title = " ".join(heading.text().split()) if heading else ""
            if not title:
                continue
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append({"url": url, "title": title, "snippet": cls._baidu_snippet(block, title)})
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _baidu_snippet(block, title: str) -> str:
        """One card's summary without depending on hashed class names.

        The card's inner classes carry build hashes (``summary-text_15QGa``), so
        the summary is matched by class *prefix*; the card's own text minus the
        title is the fallback when the vendor renames them again.
        """
        for node in block.walk():
            if any(c == "c-abstract" or c.startswith(("summary-text", "content-right"))
                   for c in node.classes):
                summary = " ".join(node.text().split())
                if summary:
                    return summary[:400]
        summary = " ".join(block.text().split())
        if title and summary.startswith(title):
            summary = summary[len(title):].strip()
        return summary[:400]

    def _search_url(self, keywords: str) -> str:
        return f"{self.endpoint}?{urllib.parse.urlencode({'wd': keywords, 'rn': '20'})}"

    def search_once(self, query: SearchQuery) -> list[SearchResult]:
        url = self._search_url(self.engine_query(query.text))
        payload, _content_type = self._request(url, self.timeout)
        return _coerce(self._parse_baidu_results(payload, query.limit), self.name)

    def search(self, query: SearchQuery) -> list[SearchResult]:
        """One page per keyword combination, merged by URL.

        The tender-intent expansions are the base class's, because this
        channel's purpose is the same as Sogou's: find the publisher's own
        notice rather than a page about the product.
        """
        texts = [self.engine_query(query.text)]
        if self._is_tender_intent(query):
            texts.extend(self.engine_query(text) for text in self._tender_queries(query))
        rows: list[Mapping[str, str]] = []
        seen: set[str] = set()
        for text in texts:
            payload, _content_type = self._request(self._search_url(text), self.timeout)
            for row in self._parse_baidu_results(payload, query.limit):
                key = row["url"].strip().casefold()
                if key in seen:
                    continue
                if self._is_tender_intent(query) and self._relevance_score(row) < 1:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) >= query.limit:
                    break
            if len(rows) >= query.limit:
                break
        return _coerce(rows[:query.limit], self.name)


_ccgp_pace_lock = threading.Lock()
_ccgp_last_request = 0.0
#: The body-read clock. Separate from ``_ccgp_last_request`` on purpose: that
#: one belongs to the search subdomain, which is the host that actually blocks
#: this project (: two direct list requests, ten minutes apart,
#: both answered with the site's own "频繁访问" page). Announcement articles
#: live on a different hostname with a different tolerance, and pacing them off
#: the search clock made every body read pay the search floor: measured
#: 2026-09-24, eight bodies took 42.34s where the network alone was 0.1-0.4s
#: each .
_ccgp_body_pace_lock = threading.Lock()
_ccgp_body_last_request = 0.0
#: A rate-limit page is a stop signal, not a retry hint. The site says so in
#: its own words ("频繁访问"), and the slow-and-compliant route this project
#: takes is to stand down rather than to keep knocking, add proxies, or solve
#: challenges. During the pause every call reports a provider error instead of
#: returning nothing, because an empty return is exactly the silent failure
#: this adapter was fixed for.
CCGP_RATE_LIMIT_COOLDOWN = 300.0
#: The pause is this site's own self-restraint, not a global outage. The
#: adapter can also be selected as a generic body reader, and standing it down
#: for unrelated hosts would be a failure this module invented itself.
CCGP_HOST_SUFFIX = "ccgp.gov.cn"
#: The one host the 6s floor still guards.
#:
#: The block measured was on the *search* subdomain: two list
#: requests, ten minutes apart, both returned the rate-limit page, while three
#: article fetches on ``www.ccgp.gov.cn`` succeeded 3/3 in the same window. The
#: 6s floor answers the first measurement and is left exactly as it was;
#: splitting the clock is what lets the second be paced by its own evidence.
CCGP_SEARCH_HOST_SUFFIX = "search.ccgp.gov.cn"
#: Floor between two *body* request starts, in seconds.
#:
#: Measured against ``www.ccgp.gov.cn`` (2026-09-24, 20:29-20:31): eight
#: articles at a 0.5s start-to-start interval returned 200 8/8, and eight more
#: at 4-way concurrency returned 200 8/8 -- no "频繁访问" page, no 403, no reset
#: . 0.5s is the tightest interval actually run, so
#: the shipped value is twice it: that measurement is a 60-request window on one
#: evening and does not license shipping its edge.
CCGP_BODY_MIN_INTERVAL = 1.0
#: Hard ceiling on body fetches in flight at once. The same measurement ran 2,
#: 3 and 4 workers clean (eight articles each); 3 is one step below the widest
#: run. It is a ceiling, not a hint -- raising it needs a fresh measurement, and
#: no caller may exceed it.
CCGP_BODY_FETCH_WORKERS = 3
#: The site's own words, as bytes so a payload can be screened without decoding
#: it ( read them out of a live 2920 B rate-limit page).
_CCGP_RATE_LIMIT_BYTES = (
    "频繁访问".encode("utf-8"), "您的访问过于频繁".encode("utf-8"),
    "频繁访问".encode("gb18030"), "您的访问过于频繁".encode("gb18030"),
)
#: A rate-limit page is ~3 KB; screening a large attachment for the marker
#: would mean reading a payload it cannot be hiding in.
CCGP_RATE_LIMIT_SCREEN_BYTES = 200_000
#: The widest date window this site answers faithfully, in days.
#:
#: Asking for more days returns *fewer* announcements, and it does so without
#: any error: measured 2026-09-23, windows of 367, 370, 400 and 630 days all
#: come back with the same 921 rows where a 365-day window returns 1526, and
#: the loss is real rather than a change of scope -- a model that matches 2
#: rows inside the year matches 1. There is no signal to catch, so the window
#: is capped instead of trusted: widening it would silently narrow the search.
#: Re-measure before raising this; the margin over the default is one day.
CCGP_TRUTHFUL_WINDOW_DAYS = 366
#: Oldest coverage one query may walk: three faithful windows.
#:
#: The cap is what keeps a wide request bounded -- at most three list requests,
#: each one paced by ``min_interval`` -- and it keeps the walk finite even if a
#: caller hands over a window with no lower bound.
CCGP_MAX_WINDOW_DAYS = CCGP_TRUTHFUL_WINDOW_DAYS * 3


def _ccgp_looks_rate_limited(payload) -> bool:
    """Whether *payload* is the site's own rate-limit page.

    Screened on bytes so a body of any encoding can be checked before it is
    decoded, and only for payloads small enough to be that page at all.
    """
    if not payload or len(payload) > CCGP_RATE_LIMIT_SCREEN_BYTES:
        return False
    return any(marker in payload for marker in _CCGP_RATE_LIMIT_BYTES)


def _ccgp_form_date(value) -> str:
    """Format one window bound the way the site's own form sends it.

    Accepts a zero-padded ISO string (the ``SearchQuery.window_start`` /
    ``window_end`` convention, ) or a ``date`` / ``datetime``.
    """
    if isinstance(value, str):
        try:
            value = datetime.strptime(value.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("window bound must be a zero-padded ISO date (YYYY-MM-DD)") from exc
    return value.strftime("%Y:%m:%d")


def ccgp_window_segments(window_start: str | None = None, window_end: str | None = None, *,
                         now=None, window_days: int = CCGP_TRUTHFUL_WINDOW_DAYS,
                         max_days: int = CCGP_MAX_WINDOW_DAYS) -> list[tuple[str, str]]:
    """Split one query window into requests the site answers faithfully.

    Returns inclusive ``(start, end)`` pairs as zero-padded ISO strings, oldest
    first, non-overlapping and contiguous. A single pair spans at most
    ``window_days`` days counted inclusively -- the site's own measured limit:
    a wider box does not error, it silently returns fewer announcements
    (``CCGP_TRUTHFUL_WINDOW_DAYS`` carries the measurement).

    Rules, all of which the callers depend on:

    * ``window_start`` / ``window_end`` are zero-padded ``YYYY-MM-DD`` strings
      ; anything else raises ``ValueError`` rather than being
      coerced, because a silently mis-parsed bound moves the whole window.
    * A missing end means "up to today"; a missing start means "as far back as
      this adapter walks". Both mean a bounded request, never an open one.
    * A window wider than ``max_days`` keeps its **most recent** part and drops
      the oldest: a procurement lead is matched against recent announcements,
      and an unbounded walk is what the cap exists to prevent. The truncation is
      silent here; the caller sees the segments it will really request.
    * ``window_start > window_end`` raises ``ValueError``.
    """
    window_days, max_days = int(window_days), int(max_days)
    if window_days < 1 or max_days < 1:
        # Both knobs are loop bounds: a non-positive one would turn the walk
        # into thousands of one-day requests, so it is refused up front.
        raise ValueError("window_days and max_days must be positive")

    def parse(value, side: str):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        text = str(value).strip()
        # Zero padding is not cosmetic: these strings are compared
        # lexicographically downstream , and ``strptime``
        # alone would quietly accept ``2024-1-1``.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"window_{side} must be a zero-padded ISO date (YYYY-MM-DD)")
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"window_{side} must be a zero-padded ISO date (YYYY-MM-DD)") from exc

    start, end = parse(window_start, "start"), parse(window_end, "end")
    reference = now or datetime.now()
    today = reference.date() if isinstance(reference, datetime) else reference
    if end is None:
        end = today
    if start is None:
        start = end - timedelta(days=max_days - 1)
    if start > end:
        raise ValueError("window_start must not be after window_end")
    start = max(start, end - timedelta(days=max_days - 1))
    span = window_days - 1
    segments: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        segment_end = min(cursor + timedelta(days=span), end)
        segments.append((cursor.isoformat(), segment_end.isoformat()))
        cursor = segment_end + timedelta(days=1)
    return segments


@dataclass
class CCGPSearchProvider(PublicWebSearchProvider):
    """China Government Procurement announcement search (search.ccgp.gov.cn).

    The site returns server-rendered HTML but its WAF blocks self-identifying
    tool user agents, so this provider sends a normal browser profile and
    paces requests to stay within the site's tolerance. Results are discovery
    leads only; ``OfficialSearchService`` still fetches the announcement body
    and classifies it before any price evidence is accepted.

    Two search modes exist on the site and the form never shows the switch:

    * ``searchtype=1`` matches the *title* only. Measured: ``生物安全柜`` 178
      rows, but a brand query and an exact model query 0 rows --
      both live in announcement bodies, never in titles.
    * ``searchtype=2`` matches the *body*. The same brand returns 10 rows whose
      titles do not contain it at all (a 2026 laboratory-equipment award notice
      whose title only names the buyer), and ``生物安全柜`` returns
      1526 rows against the title mode's 178.

    This adapter therefore sends ``searchtype=2``. That is what makes the model
    and the brand searchable at all: under the title mode a model query was
    guaranteed to return zero rows while still answering HTTP 200, and the run
    recorded it as a healthy, empty channel.

    Body indexing splits identifiers, so a full model string is unreliable: of
    five model strings, all five matched
    exactly, while two longer model strings matched nothing yet
    their alphabetic prefixes ``TBSPX``/``SPX`` did. ``query_ladder`` walks
    from the exact model outward, most precise first, and stops at the first
    keyword that returns rows.

    Pacing is split by host, not by page type, because that is what the two
    measurements describe. ``search.ccgp.gov.cn`` keeps the original 6s floor
    (``min_interval``) on its own module-level clock: that host answered the
    very first request with a rate-limit page  and the floor
    is the answer to it. Article bodies -- ``www.ccgp.gov.cn/cggg/...`` -- are
    paced by ``body_min_interval`` on a second, independent clock, because that
    host was measured to tolerate far more (see ``CCGP_BODY_MIN_INTERVAL``).
    The cooldown after a rate-limit page is shared by both: a stop signal is a
    stop signal, whichever line hit it.
    """

    name: str = "ccgp"
    endpoint: str = "http://search.ccgp.gov.cn/bxsearch"
    timeout: float = 20.0
    min_interval: float = 6.0
    #: Floor for announcement bodies. See ``CCGP_BODY_MIN_INTERVAL`` for the
    #: measurement; the field exists so a caller can be stricter, not looser.
    body_min_interval: float = CCGP_BODY_MIN_INTERVAL
    years_window: float = 1.0
    purposes: frozenset[str] = frozenset({"tender", "procurement", "award", "price"})
    rate_limit_cooldown: float = CCGP_RATE_LIMIT_COOLDOWN
    #: Monotonic deadline set when the site answers with its rate-limit page.
    paused_until: float = 0.0
    #: Keyword actually sent by the last ``search_once``. The ladder may walk
    #: past the first candidate, and the run's telemetry has to report the one
    #: that was really sent rather than the one that was preferred.
    last_engine_query: str = ""
    #: The site's other two form filters. They have no ``SearchQuery`` field yet
    #: , so they are channel configuration; an empty value keeps the
    #: parameter out of the URL entirely.
    agent_name: str = ""
    display_zone: str = ""

    _MARKER_WORDS = (
        "中标", "成交", "单价", "采购结果", "采购", "招标", "合同", "代理",
        "科研院所", "结果公告", "公告", "公开", "询价", "竞争性", "预算",
    )

    def supports(self, query: SearchQuery) -> bool:
        if query.purpose.casefold() not in self.purposes:
            return False
        # A query with nothing left to send must not be counted as a healthy
        # empty search. Under body search that is a much narrower set than it
        # used to be: a model or a brand alone is now worth sending.
        if self.query_ladder(query.text, brand=getattr(query, "brand", None),
                             model=getattr(query, "model", None)):
            return True
        # See ``_filter_only_keywords``: a query that carries the site's own
        # exact filter is searchable even with no keyword candidate left.
        return bool(self._filter_only_keywords(query))

    def _filter_only_keywords(self, query: SearchQuery) -> list[str]:
        """The one request a filter-only query is worth: the filter, no keyword.

        ``projectId`` and ``buyerName`` are exact filters rather than keywords,
        so a query carrying one of them is searchable even when every keyword
        candidate was stripped -- a project id is not a Chinese category word,
        and the ladder only ever produces Chinese words and identifiers.

        The keyword is deliberately left empty instead of sending the leftover
        text. Measured live (2026-09-24, ``projectId=2921-20250429-667``,
        window 2025-01-01..2025-12-31): empty keyword **2 rows**, keyword set to
        the project id **1 row** -- the body index does not hold the identifier
        as one token, so ANDing it drops one of the two announcements the exact
        filter had already found.  #2/#3 measured the same two
        filters without a keyword for the same reason.

        A query with no exact filter returns nothing here, so an ordinary
        unsearchable query still leaves the adapter unsent.
        """
        if not (getattr(query, "project_id", None) or getattr(query, "buyer_name", None)):
            return []
        return [""]

    @classmethod
    def engine_query(cls, text: str, *, brand: str | None = None, model: str | None = None) -> str:
        """The most precise keyword this adapter would send for the query."""
        ladder = cls.query_ladder(text, brand=brand, model=model)
        return ladder[0] if ladder else ""

    @classmethod
    def query_ladder(cls, text: str, *, brand: str | None = None, model: str | None = None) -> list[str]:
        """Candidate keywords for one query, most precise first.

        Body indexing splits identifiers, so the exact model sometimes misses
        where its alphabetic prefix hits, and sometimes the reverse: ``ZQLY``
        matched ten rows that were all an agency's project codes
        (``ZQLY-CS-202621``) while ``ZQLY-180`` matched the one real notice.
        No single ordering serves both, so ``search_once`` walks this list and
        stops at the first keyword that returns rows.
        """
        candidates: list[str] = []

        def add(value: str) -> None:
            token = re.sub(r"\s+", " ", str(value or "")).strip()
            if not token:
                return
            if token.casefold() in {c.casefold() for c in candidates}:
                return
            candidates.append(token[:60])

        model_token = str(model or "").strip()
        if model_token:
            add(model_token)
            # The prefix is the fallback for a model the index split. Below
            # three letters it matches far too much to be worth a request.
            prefix = re.match(r"[A-Za-z]{3,}", model_token)
            if prefix:
                add(prefix.group(0))
        add(brand)
        add(cls.category_word(cls._without_identifiers(text, brand, model)))
        return candidates

    @staticmethod
    def _without_identifiers(text: str, brand: str | None, model: str | None) -> str:
        value = str(text or "")
        for identifier in (brand, model):
            token = str(identifier or "").strip()
            if token:
                value = re.sub(re.escape(token), " ", value, flags=re.I)
        return value

    @classmethod
    def category_word(cls, text: str) -> str:
        """The longest Chinese run left once operators and markers are gone.

        One Chinese word is the only keyword shape measured to behave; a longer
        box is not gambled on.
        """
        value = re.sub(r'\b(?:site|filetype|inurl|intitle|link):[^\s]+', ' ', str(text or ''), flags=re.I)
        value = re.sub(r'-\u201c[^\u201d]+\u201d|-"[^"]+"', ' ', value)
        value = re.sub(r'[\u201c\u201d"\']', ' ', value)
        for marker in cls._MARKER_WORDS:
            value = value.replace(marker, ' ')
        candidates = re.findall(r'[\u3400-\u9fff]{2,}', value)
        if not candidates:
            return ''
        return max(candidates, key=len)[:60]

    def _request_headers(self, url: str) -> dict[str, str]:
        headers = super()._request_headers(url)
        if (urllib.parse.urlsplit(url).hostname or "").endswith("ccgp.gov.cn"):
            headers["Referer"] = "http://search.ccgp.gov.cn/"
        return headers

    def _request(self, url: str, timeout: float, **form) -> tuple[bytes, str | None]:
        host = (urllib.parse.urlsplit(url).hostname or '').lower()
        on_ccgp = host.endswith(CCGP_HOST_SUFFIX)
        if (self.paused_until and time.monotonic() < self.paused_until and on_ccgp):
            # Stop means stop for this site: after the site's rate-limit page
            # this adapter sends nothing more to it. Reported as a provider
            # failure, never as an empty result -- the caller has to see that
            # this channel did not work this round instead of reading an empty
            # search as a healthy one. The pause lives on the instance, so it
            # covers both the list request and every detail fetch that follows.
            raise ProviderConfigurationError('ccgp paused after a rate-limit page (频繁访问); no further request this round')
        if self.transport is not None:
            return self.transport(url, timeout)
        if host.endswith(CCGP_SEARCH_HOST_SUFFIX):
            # The list-search line, unchanged: same lock, same global, same
            # formula, same 6s floor. It is the one that must not be loosened.
            global _ccgp_last_request
            with _ccgp_pace_lock:
                wait = _ccgp_last_request + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _ccgp_last_request = time.monotonic()
        else:
            # Bodies run on their own clock. A caller running several fetches at
            # once still serialises their *starts* here, which is what keeps a
            # worker pool from turning into a burst.
            global _ccgp_body_last_request
            with _ccgp_body_pace_lock:
                wait = _ccgp_body_last_request + self.body_min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _ccgp_body_last_request = time.monotonic()
        payload, content_type = super()._request(url, timeout, **form)
        if on_ccgp and _ccgp_looks_rate_limited(payload):
            # A body read can land on the rate-limit page too, and until now
            # that page was returned as if it were the article. Arming the same
            # cooldown here is what makes "stop on the first rate-limit page"
            # true for body reads, not just for list searches.
            self.paused_until = time.monotonic() + self.rate_limit_cooldown
            raise ProviderConfigurationError('ccgp rate limited (频繁访问)')
        return payload, content_type

    def _search_url(self, keywords: str, page: int = 1, *, start=None, end=None,
                    buyer_name=None, project_id=None,
                    agent_name=None, display_zone=None) -> str:
        """Build one site-search URL.

        The four form filters are all optional and all off by default. Measured
        on the live site: ``projectId`` matches one project exactly and returns
        only its announcements, and ``buyerName`` + one ``kw`` is an AND pair
        ( #3/#4) -- that pair is what the alignment chain asks
        with, because a brand word beats a model string on this index
        (``1300A2`` full-text returned 2 unrelated projects where ``知楚``
        returned the announcements carrying the brand).

        ``start`` / ``end`` take a zero-padded ISO string or a ``datetime``.
        Both omitted, the historic default window is computed exactly as before,
        so a caller that passes nothing gets a byte-identical URL -- that is the
        compatibility contract for the existing ``SearchQuery`` call sites. One
        bound alone is refused: completing it from "now" would move the window
        under the caller.
        """
        if start is None or end is None:
            # One bound alone would put the other one wherever "now" happens to
            # be, so it is refused rather than quietly completed.
            if (start is None) != (end is None):
                raise ValueError("start and end must be given together")
            end = datetime.now()
            start = end - timedelta(days=min(round(365 * self.years_window),
                                            CCGP_TRUTHFUL_WINDOW_DAYS))
        params = {
            "searchtype": "2", "page_index": str(page), "bidSort": "0",
            "buyerName": str(buyer_name or ""), "projectId": str(project_id or ""),
            "pinMu": "0", "bidType": "0", "dbselect": "bidx", "kw": keywords,
            "timeType": "6",
            "start_time": _ccgp_form_date(start), "end_time": _ccgp_form_date(end),
        }
        # Empty filters stay out of the URL rather than travelling as empty
        # parameters: the pre-existing URL has no agentName/displayZone at all.
        if agent_name:
            params["agentName"] = str(agent_name)
        if display_zone:
            params["displayZone"] = str(display_zone)
        return f"{self.endpoint}?{urllib.parse.urlencode(params)}"

    def _query_filters(self, query: SearchQuery) -> dict[str, str]:
        """The site-form filters this query carries.

        ``buyer_name`` / ``project_id`` come from the query ;
        ``agent_name`` / ``display_zone`` are channel configuration until the
        model grows those fields. CCGP has no supplier parameter, so
        ``supplier_name`` cannot travel here -- it belongs in ``kw``, where the
        site ANDs it with ``buyerName``.
        """
        return {
            "buyer_name": getattr(query, "buyer_name", None),
            "project_id": getattr(query, "project_id", None),
            "agent_name": self.agent_name,
            "display_zone": self.display_zone,
        }

    def _window_segments(self, query: SearchQuery) -> list[tuple[str, str]] | None:
        """Segments to walk for this query, or ``None`` for the historic request.

        ``None`` means the caller set no window at all. The adapter then sends
        exactly the single request it always sent, ``years_window`` included.
        """
        start = getattr(query, "window_start", None)
        end = getattr(query, "window_end", None)
        if not start and not end:
            return None
        return ccgp_window_segments(start, end)

    @staticmethod
    def _parse_ccgp_results(payload: bytes, limit: int) -> list[Mapping[str, str]]:
        from backend.infrastructure.search_markup import Node, ResultTree
        text = payload.decode("utf-8", errors="replace")
        # The wording is the site's own, read from a live rate-limited reply:
        # "<title>频繁访问!中国政府采购网</title>".
        if "频繁访问" in text or "您的访问过于频繁" in text:
            raise ProviderConfigurationError("ccgp rate limited (频繁访问)")
        rows: list[Mapping[str, str]] = []
        for block in ResultTree(text).root.walk():
            if block.tag != "ul" or "vT-srch-result-list-bid" not in block.classes:
                continue
            for item in block.children:
                if not isinstance(item, Node) or item.tag != "li":
                    continue
                anchor = next((n for n in item.walk() if n.tag == "a"
                               and (n.attrs.get("href") or "").startswith(("http://", "https://"))), None)
                if anchor is None:
                    continue
                paragraph = next((n for n in item.walk() if n.tag == "p"), None)
                meta = [re.sub(r"\s+", " ", n.text()).strip() for n in item.walk() if n.tag == "strong"]
                stamp = next((" ".join(n.text().split()) for n in item.walk()
                              if n.tag == "span" and re.match(r"\d{4}[.\-/]\d{1,2}", n.text().strip())), "")
                parts = [" ".join(paragraph.text().split()) if paragraph else ""]
                parts.extend(m for m in meta if m)
                if stamp:
                    parts.append(stamp)
                snippet = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
                rows.append({"url": anchor.attrs["href"], "title": " ".join(anchor.text().split()), "snippet": snippet})
                if len(rows) >= limit:
                    return rows
        return rows

    def search_once(self, query: SearchQuery) -> list[SearchResult]:
        ladder = self.query_ladder(query.text, brand=getattr(query, "brand", None),
                                   model=getattr(query, "model", None))
        # A query that carries the site's own exact filter is worth sending even
        # when no keyword candidate survived (see ``_filter_only_keywords``).
        candidates = ladder or self._filter_only_keywords(query)
        # Reported by the run's telemetry, so it must name a keyword that was
        # really sent: the first one when nothing matched, otherwise the one
        # that did.
        self.last_engine_query = candidates[0] if candidates else ""
        segments = self._window_segments(query)
        if not segments or len(segments) == 1:
            # One request window: the historic path, unchanged. An absent window
            # leaves ``start`` / ``end`` unset so ``_search_url`` keeps
            # ``years_window`` behavior too.
            start, end = segments[0] if segments else (None, None)
            for keywords in candidates:
                payload, _content_type = self._request(
                    self._search_url(keywords, start=start, end=end,
                                     **self._query_filters(query)),
                    self.timeout)
                try:
                    rows = self._parse_ccgp_results(payload, query.limit)
                except ProviderConfigurationError:
                    self.paused_until = time.monotonic() + self.rate_limit_cooldown
                    raise
                if rows:
                    self.last_engine_query = keywords
                    return _coerce(rows, self.name)
            return []
        return self._search_windows(query, candidates, segments)

    def _search_windows(self, query: SearchQuery, candidates: list[str],
                        segments: list[tuple[str, str]]) -> list[SearchResult]:
        """Walk every <=366-day segment and merge the rows by URL.

        The site silently narrows a window wider than ``CCGP_TRUTHFUL_WINDOW_DAYS``
        instead of erroring, so a wide window is asked for in pieces and the
        pieces are merged. Per segment the keyword rule is the single-window
        rule: the ladder stops at its first keyword that returns rows.

        A rate-limit page stops the whole round. The segments after it are not
        attempted and nothing is retried -- the pause is armed and the site's
        own error propagates, because an empty return here is exactly the silent
        failure this adapter was fixed for. Every request still goes through
        ``_request``, so the shared 6s floor and the pause apply as usual.
        """
        merge: list[Mapping[str, str]] = []
        seen: set[str] = set()
        filters = self._query_filters(query)
        for start, end in segments:
            rows: list[Mapping[str, str]] = []
            for keywords in candidates:
                payload, _content_type = self._request(
                    self._search_url(keywords, start=start, end=end, **filters),
                    self.timeout)
                try:
                    rows = self._parse_ccgp_results(payload, query.limit)
                except ProviderConfigurationError:
                    self.paused_until = time.monotonic() + self.rate_limit_cooldown
                    raise
                if rows:
                    self.last_engine_query = keywords
                    break
            for row in rows:
                key = str(row.get("url", "")).strip().casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                merge.append(row)
            if len(merge) >= query.limit:
                # The caller asked for this many; further segments would spend
                # the site's pacing without adding a row the caller will keep.
                break
        return _coerce(merge[:query.limit], self.name)

    def search(self, query: SearchQuery) -> list[SearchResult]:
        return self.search_once(query)


@dataclass
class LocalIndexSearchProvider:
    """Small deterministic provider for fixtures or a local pre-built index."""
    name = "local_index"
    def __init__(self, records: Iterable[SearchResult | Mapping]):
        self.records = _coerce(records, self.name)

    def supports(self, query: SearchQuery) -> bool: return True
    def search(self, query: SearchQuery) -> list[SearchResult]:
        terms = {x.casefold() for x in query.text.replace('"', ' ').split() if len(x) > 1}
        ranked = []
        for row in self.records:
            haystack = f"{row.title} {row.snippet} {row.url}".casefold()
            score = sum(term in haystack for term in terms)
            if score: ranked.append((score, row))
        return [row for _, row in sorted(ranked, key=lambda x: (-x[0], x[1].url))[:query.limit]]
