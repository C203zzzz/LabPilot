"""Bounded public-page reader independent of search providers.

HTTP bytes and browser-rendered DOM have distinct provenance. The browser
uses an ephemeral context with no user cookies, public DNS checks on every
request/redirect, one navigation and a single bounded readiness wait.
"""
from dataclasses import dataclass, field
import os
import re
import threading
import time
import urllib.parse
from urllib.error import HTTPError
from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.services.research_sources import network_url, parse_web_url

_browser_lock = threading.Lock()

#: 渲染是为了取回页面脚本才填上的下载地址，一次会话里同一个 URL 只做一次。
#: 这里存的是「已经渲染过的页面」，命中即复用；失败也记进去，否则同一页面
#: 会在每个来源被重新启动一次浏览器。
_download_render_lock = threading.Lock()
_download_render_cache: dict = {}
_download_render_limit = 32
_render_failed = object()


def _anchors(html):
    """逐个产出 ``(属性串, 标签内文本)``；解析只要够用，不求完整 DOM。"""
    for match in re.finditer(r'<a\b([^>]*)>([\s\S]*?)</a\s*>', html or '', re.I):
        yield match.group(1), match.group(2)


def _anchor_href(attributes):
    """锚点的 href 值；没有任何 URL 时返回 ``None``。

    ``href=''`` 和根本没有 href 属性都是「没有 URL」：发布方的前端模板两种
    都写，而这两种情况下正文里剩下的只有文件名。
    """
    match = re.search(r'\bhref\s*=\s*["\']([^"\']*)["\']', attributes, re.I)
    if match is None:
        return None
    return match.group(1).strip() or None


#: 下载语义：类名、title 或可见文字里有「下载」的意思。判定只用这条，
#: 不去猜任何站点的地址规律——地址是什么由渲染后的 DOM 说了算。
_DOWNLOAD_SEMANTICS = re.compile(r'download|下载', re.I)


def _has_download_semantics(attributes, inner):
    return bool(_DOWNLOAD_SEMANTICS.search(attributes + ' ' + re.sub(r'<[^>]+>', ' ', inner)))


def unresolved_download_anchors(html):
    """静态 HTML 里「有下载语义、却没有 URL」的锚点个数。

    命中说明这个页面的文件列表必须渲染后才能取到地址：正文里会留下文件名，
    却没有一个可以请求的地址，抽取层于是报「没有附件」——而附件就在那儿。
    """
    return sum(1 for attributes, inner in _anchors(html)
               if _anchor_href(attributes) is None and _has_download_semantics(attributes, inner))


def resolved_download_links(html, base_url='', skipped=None):
    """渲染后的 DOM 里，下载语义锚点真正指向的地址（绝对化、去重、保序）。

    ``href`` 是发布方页面上的任意字符串：主机名尾巴带全角标点的那种畸形链接
    会让 ``urljoin``/``urlsplit`` 抛 ``ValueError``（标准库
    ``urllib/parse.py:_checknetloc``，防 IDN 同形字）。这里和
    ``research_sources.public_url`` 一样 fail-closed——**这一个链接不可用**，
    跳过它继续看后面的，而不是让整页、整条来源、整条车道失败。
    ``skipped`` 是可选的留痕列表，把读不出来的原始 ``href`` 原样记下来。
    """
    found = []
    for attributes, inner in _anchors(html):
        href = _anchor_href(attributes)
        if href is None or not _has_download_semantics(attributes, inner):
            continue
        url = parse_web_url(href, base_url)
        if url is None:
            if skipped is not None:
                skipped.append({'raw': href, 'reason': 'unparseable'})
            continue
        if urllib.parse.urlsplit(url).scheme.lower() not in {'http', 'https'}:
            continue
        if url not in found:
            found.append(url)
    return found


class PageReadError(ValueError):
    def __init__(self, kind, status=None):
        super().__init__(kind)
        self.kind, self.status = kind, status


def terminal_failure(exc):
    """HTTP and browser failures use the same non-retryable classification."""
    status = getattr(exc, 'status', None) or getattr(exc, 'code', None)
    return status in {401, 403, 404, 410, 412, 419} or getattr(exc, 'kind', '') in {
        'verification_required', 'login_required', 'not_found', 'page_too_large',
    }


#: Failures no other transport can answer. This is deliberately *not* the same
#: question as ``terminal_failure``: that one classifies the source outcome (it
#: decides ``unavailable`` vs ``failed`` in the research chain), while this one
#: decides whether the browser deserves the same URL. A WAF/JS challenge is
#: terminal for plain HTTP and is exactly what a browser exists to get past, so
#: the two must stay separate predicates (handoff 9.2).
_HOPELESS_KINDS = frozenset({'login_required', 'not_found', 'page_too_large'})
_HOPELESS_STATUSES = frozenset({401, 404, 410, 412, 419})


def escalatable_failure(exc):
    """Whether another transport could plausibly answer the same URL."""
    kind = getattr(exc, 'kind', '') or type(exc).__name__
    status = getattr(exc, 'status', None) or getattr(exc, 'code', None)
    return kind not in _HOPELESS_KINDS and status not in _HOPELESS_STATUSES


def quality(body):
    text = body.strip()
    if re.search(r'404\s*(?:not found|错误)|页面不存在|页面已删除', text[:500], re.I):
        return 'not_found'
    if re.search(r'(?:enable|without) javascript|开启JavaScript|启用JavaScript', text, re.I) and len(text) < 1200:
        return 'empty_shell'
    if re.search(r'验证码|人机验证|access denied|verify you are human|checking your browser|安全验证', text[:1800], re.I):
        return 'verification_required'
    if len(text) < 1500 and re.search(r'请.*登录|统一身份认证|登录后.*查看', text):
        return 'login_required'
    if len(text) < 100:
        return 'empty_shell'
    return None


@dataclass
class PageResult:
    payload: bytes
    content_type: str
    text: str
    metadata: dict = field(default_factory=dict)


class PublicPageReader:
    name = 'public_http_browser'
    def __init__(self, browser=False):
        self.http = PublicWebSearchProvider()
        self.browser = browser

    def read_http(self, url, timeout=15, referer=None):
        start = time.monotonic()
        # *referer* is the page this URL was linked from. Some publishers (CCGP's
        # ``download.ccgp.gov.cn`` attachments) reject a request that arrives
        # with no referrer at all; a run that reached the file from a search
        # result rather than from its notice used to look like a site refusing
        # us. Passing the known parent page is the ordinary browser path. With
        # ``None`` the provider falls back to its own rule, which today only
        # names that one download host -- every other read keeps its old headers.
        payload, content_type = self.http.for_referer(referer).fetch_raw(url, timeout)
        body = '' if payload.startswith(b'%PDF') else self.http.decode_body(payload, content_type, url)
        problem = quality(body) if not payload.startswith(b'%PDF') else None
        if problem:
            raise PageReadError(problem, 200)
        metadata = {'reader': 'http', 'snapshot_kind': 'response_bytes',
                    'status_code': 200, 'requested_url': url, 'elapsed_seconds': round(time.monotonic()-start, 3)}
        if not payload.startswith(b'%PDF'):
            # 这一遍拿不到地址的下载锚点，在这里就记下来：调用方据此决定
            # 要不要为这个页面渲染一次，而不是等抽取层报「没有附件」。
            html = payload.decode('utf-8', 'replace')
            metadata['unresolved_downloads'] = unresolved_download_anchors(html)
            # 留痕里只有一个计数时，事后没人知道这一页原本挂着哪几条地址、
            # 其中哪一条根本读不出来。静态那一遍能读出来的地址与读不出来的原始
            # ``href`` 都留一份（都有上限），出问题时不必再把页面重抓一遍。
            unparseable = []
            metadata['download_links'] = resolved_download_links(html, url, skipped=unparseable)[:20]
            if unparseable:
                metadata['unparseable_downloads'] = unparseable[:10]
        return PageResult(payload, content_type, body, metadata)

    def read_browser(self, url, timeout=25, referer=None):
        from playwright.sync_api import sync_playwright
        network_url(url)
        start = time.monotonic()
        with _browser_lock, sync_playwright() as runtime:
            browser = runtime.chromium.launch(channel=os.getenv('LAB_BROWSER_CHANNEL', 'msedge'), headless=True)
            try:
                # 无头浏览器自报的 UA 里带 ``HeadlessChrome``，政府采购站点据此
                # 直接 403（实测 CCGP 详情页：默认 UA 403，普通 Chrome UA 200）。
                # 用的是与 HTTP 那一遍同一个**常规浏览器标识**——版本号只在
                # ``PublicWebSearchProvider._request_headers`` 一处维护，这里不写死
                # 版本；需要换标识时用 ``LAB_BROWSER_USER_AGENT`` 覆盖。
                # 这不是反爬绕过：页面只要是验证码页，下面的 ``quality`` 照旧
                # 判成 ``verification_required`` 并让这次读取失败。
                profile = self.http.for_referer(referer)._request_headers(url)
                rendered_headers = {'Accept-Language': profile['Accept-Language']}
                if profile.get('Referer'):
                    # The rendered pass navigates the same URL the HTTP pass just
                    # read, so it carries the same referrer; without it the
                    # browser's own default (this page's URL) is what CCGP's
                    # download host sees, and the request is refused again.
                    rendered_headers['Referer'] = profile['Referer']
                context = browser.new_context(
                    service_workers='block', accept_downloads=False,
                    user_agent=os.getenv('LAB_BROWSER_USER_AGENT') or profile['User-Agent'],
                    locale='zh-CN',
                    extra_http_headers=rendered_headers)

                checked = set()
                def route_request(route):
                    request = route.request
                    if request.resource_type in {'image', 'media', 'font'}:
                        return route.abort()
                    try:
                        if request.url not in checked:
                            network_url(request.url)
                            checked.add(request.url)
                    except (ValueError, OSError):
                        return route.abort()
                    return route.continue_()
                context.route('**/*', route_request)
                page = context.new_page()
                response = page.goto(url, wait_until='domcontentloaded', timeout=min(timeout, 30)*1000)
                status = response.status if response else None
                if status is None or status >= 400:
                    raise PageReadError('http_' + str(status), status)
                # Like Crawl4AI's bounded content readiness, wait for rendered
                # text to settle; do not depend on networkidle (analytics poll).
                body, html, previous, stable = '', '', None, 0
                ready_deadline = min(start + timeout, time.monotonic() + 6)
                while True:
                    network_url(page.url)
                    html = page.content()
                    if len(html.encode('utf-8')) > 16*1024*1024:
                        raise PageReadError('page_too_large', status)
                    body = self.http._clean_html(html, page.url)
                    problem = quality(body)
                    if problem in {'verification_required', 'login_required', 'not_found'}:
                        raise PageReadError(problem, status)
                    stable = stable + 1 if body == previous else 0
                    if (not problem and stable >= 3) or time.monotonic() >= ready_deadline:
                        break
                    previous = body
                    page.wait_for_timeout(500)
                network_url(page.url)
                payload = html.encode('utf-8')
                if len(payload) > 16*1024*1024:
                    raise PageReadError('page_too_large', status)
                body = self.http._clean_html(html, page.url)
                problem = quality(body)
                if problem:
                    raise PageReadError(problem, status)
                rendered_unparseable = []
                rendered_downloads = resolved_download_links(html, page.url, skipped=rendered_unparseable)
                rendered_metadata = {'reader': 'browser', 'snapshot_kind': 'rendered_dom', 'status_code': status,
                                     'requested_url': url, 'final_url': page.url,
                                     'resolved_downloads': rendered_downloads,
                                     'elapsed_seconds': round(time.monotonic()-start, 3)}
                if rendered_unparseable:
                    # 畸形 ``href`` 现在被跳过了。不记下来就等于把"这一页原本有一条
                    # 读不出来的下载地址"整件事抹掉。
                    rendered_metadata['unparseable_downloads'] = rendered_unparseable[:10]
                return PageResult(payload, 'text/html; charset=utf-8', body, rendered_metadata)
            finally:
                browser.close()

    def read_resolved_downloads(self, url, timeout=25, on_attempt=None):
        """渲染 *url* 一次，把页面脚本填上的下载地址取回来。

        只在这个页面**已经**被判为与当前比价相关、并且静态 HTML 里确实存在
        「有下载语义、没有 URL」的锚点时才值得调用——判据在调用方（见
        ``procurement_research.fetch_attachments``），这里不自己放宽。

        同一个 URL 一次会话最多渲染一次：无论成败都进缓存，重复遇到直接复用，
        不会为同一个页面第二次启动浏览器。
        """
        with _download_render_lock:
            if url in _download_render_cache:
                cached = _download_render_cache[url]
                if cached is _render_failed:
                    raise PageReadError('download_render_cached_failure')
                return cached
        if on_attempt:
            on_attempt()
        try:
            result = self.read_browser(url, timeout)
        except Exception:
            with _download_render_lock:
                _download_render_cache.setdefault(url, _render_failed)
                while len(_download_render_cache) > _download_render_limit:
                    _download_render_cache.pop(next(iter(_download_render_cache)))
            raise
        with _download_render_lock:
            _download_render_cache[url] = result
            while len(_download_render_cache) > _download_render_limit:
                _download_render_cache.pop(next(iter(_download_render_cache)))
        return result

    def read(self, url, timeout=15, on_attempt=None, referer=None):
        if on_attempt:
            on_attempt()
        try:
            return self.read_http(url, timeout, referer)
        except Exception as exc:
            kind = getattr(exc, 'kind', type(exc).__name__)
            # A challenge page or a bare 403 is exactly what a real browser gets
            # past, so it must escalate; a login wall, a missing document or an
            # oversized payload will not improve with another transport. The
            # source-outcome classification above is a different question and is
            # left to ``terminal_failure`` in the research chain.
            if not self.browser or not escalatable_failure(exc):
                raise
            if on_attempt:
                on_attempt()
            # Positional: a reader subclass or a test double that predates the
            # referrer argument keeps its exact two-argument call shape.
            result = self.read_browser(url, min(30, max(20, timeout)), referer)
            result.metadata['http_failure'] = kind
            return result


class FirecrawlPageReader:
    """Optional operator adapter, unverified against live service in this project."""
    name = 'firecrawl_unverified'
    def __init__(self, endpoint, api_key):
        self.endpoint, self.api_key = endpoint, api_key

    def read(self, url, timeout=30, on_attempt=None):
        import json
        import urllib.request
        network_url(url)
        if on_attempt:
            on_attempt()
        request = urllib.request.Request(self.endpoint.rstrip('/') + '/v2/scrape',
            data=json.dumps({'url': url, 'formats': ['markdown', 'rawHtml', 'links'], 'maxAge': 0}).encode(),
            headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(16*1024*1024+1)
        if len(raw) > 16*1024*1024:
            raise PageReadError('page_too_large')
        data = json.loads(raw).get('data') or {}
        metadata = data.get('metadata') or {}
        network_url(metadata.get('url') or metadata.get('sourceURL') or url)
        html = data.get('rawHtml') or ''
        text = PublicWebSearchProvider._clean_html(html, url)
        status = metadata.get('statusCode')
        problem = quality(text)
        if status != 200 or problem:
            raise PageReadError(problem or 'http_' + str(status), status)
        return PageResult(html.encode(), 'text/html; charset=utf-8', text,
            {'reader': self.name, 'snapshot_kind': 'provider_html', 'provider_metadata': metadata,
             'markdown': data.get('markdown'), 'links': data.get('links'), 'status_code': status})
