"""Provider-neutral M04 search orchestration; snippets never become facts."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
import threading
from urllib.parse import urlsplit
from backend.domain.search import BodyStatus, PriceEvidenceLevel, SearchProvider, SearchQuery, SearchResult, SourceRecord, SourceType, normalize_url
from backend.infrastructure.search_providers import CCGP_BODY_FETCH_WORKERS
from backend.services.lead_alignment import (
    STATE_REJECTED,
    STATE_UNRESOLVED,
    AlignmentVerdict,
    coerce_lead,
    lead_text_match,
    verify_against_lead,
)

_TENDER_BODY_MARKERS = ("招标", "中标", "成交", "采购结果", "采购公告", "采购合同", "合同明细", "合同", "评标", "开标", "tender", "award", "procurement", "contract", "bid notice")


def _supports(provider, query: SearchQuery) -> bool:
    """Whether *provider* claims to serve *query*; unusable claims are ``False``."""
    supports = getattr(provider, 'supports', None)
    if not callable(supports):
        return False
    try:
        return bool(supports(query))
    except Exception:
        return False


def _tender_body_verified(source: SourceRecord) -> bool:
    if source.body_status is not BodyStatus.FETCHED or not source.body_text:
        return False
    # A title or URL can be misleading; the formal-price gate requires the
    # fetched body itself to contain procurement semantics.
    haystack = source.body_text.casefold()
    return any(marker.casefold() in haystack for marker in _TENDER_BODY_MARKERS)


def _extract_tender_metadata(source: SourceRecord) -> None:
    """Extract explicit historical tender metadata from fetched body text."""
    body = source.body_text or ""
    if not body:
        return
    source.tender_title = source.title.strip() or None
    date_match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", body)
    if date_match:
        source.publication_date = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
    if any(token in body for token in ("人民币", "元", "￥", "¥")):
        source.currency = "CNY"
    if "不含税" in body:
        source.tax_condition = "不含税"
    elif "含税" in body:
        source.tax_condition = "含税"
    patterns = (
        r"(?:中标|成交)[^。；\n]{0,24}?金额(?:[（(]\s*元\s*[）)])?[\s：:|｜]*(?:报价)?[：:\s|｜]*[￥¥]?\s*([\d,]+(?:\.\d+)?)\s*[（(]?([万千百]?元)[）)]?",
        r"(?:总中标金额|中标（成交）金额|中标\s*金额)\s*[\s：:|｜]*[￥¥]?\s*([\d,]+(?:\.\d+)?)\s*([万千百]?元)",
    )
    # An amount printed beside a void/terminated decision is a revoked quote,
    # not a concluded historical price.
    void_markers = ("废标", "中标无效", "成交无效", "采购活动终止", "项目终止")
    for pattern in patterns:
        for match in re.finditer(pattern, body):
            # Real award pages state revocation decisions in the same
            # linearized table row, sometimes well past the amount cell.
            window = body[max(0, match.start() - 80):match.end() + 200]
            if any(marker in window for marker in void_markers):
                continue
            try:
                amount = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if amount <= 0:
                continue
            unit = match.group(2)
            source.price_amount = amount * {"万元": 10000, "千元": 1000, "百元": 100, "元": 1}.get(unit, 1)
            source.price_unit = unit
            source.price_quote = match.group(0).strip()
            break
        if source.price_amount is not None:
            break

def classify_source(url: str, title: str = "") -> SourceType:
    from backend.services.source_authority import AGGREGATOR_HOST_MARKERS, MARKETPLACE_HOST_MARKERS
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path.lower()
    if any(x in host for x in ("google.", "bing.", "baidu.")): return SourceType.SEARCH_SNIPPET
    if any(x in host for x in MARKETPLACE_HOST_MARKERS): return SourceType.DEALER
    if any(x in host for x in AGGREGATOR_HOST_MARKERS): return SourceType.AGGREGATOR
    # ccgp.gov.cn publishes procurement announcements exclusively; every page
    # there is a tender-family document regardless of pinyin path segments.
    if host == "ccgp.gov.cn" or host.endswith(".ccgp.gov.cn"): return SourceType.TENDER_DOCUMENT
    if any(x in path for x in ("tender", "招标", "采购结果", "中标", "contract")) or any(
        x in f"{title} {url}".lower() for x in ("招标", "采购结果", "采购合同", "合同明细", "中标公告", "成交公告", "中标结果", "成交结果", "结果公告", "tender", "contract", "award notice")
    ): return SourceType.TENDER_DOCUMENT
    known = ("thermofisher.com", "eppendorf.com", "agilent.com", "bio-rad.com", "tecan.com", "hach.com", "merck.com", "sigmaaldrich.com", "sigma-aldrich.com")
    official = any(host == domain or host.endswith('.' + domain) for domain in known)
    if official and (path.endswith(('.pdf', '.doc', '.docx')) or any(x in path for x in ('download', 'datasheet', 'manual', 'brochure'))):
        return SourceType.MANUFACTURER_DOWNLOAD
    return SourceType.MANUFACTURER_WEBSITE if official else SourceType.UNKNOWN

def authority(source_type: SourceType) -> tuple[int, str]:
    values = {SourceType.MANUFACTURER_DOWNLOAD:(100,"制造商下载页，通常包含可定位的原始说明书/数据表"), SourceType.MANUFACTURER_WEBSITE:(90,"制造商官网一手产品页面"), SourceType.TENDER_DOCUMENT:(85,"已完成招标/采购结果文件，可作为历史成交价证据"), SourceType.DEALER:(45,"经销商页面，可能有转述或配置差异"), SourceType.AGGREGATOR:(25,"聚合站/转载页面，来源链不完整"), SourceType.SEARCH_SNIPPET:(5,"搜索摘要仅用于发现线索，不能作为产品事实"), SourceType.UNKNOWN:(10,"来源类型无法确认")}
    return values[source_type]

def relevance_score(query: SearchQuery, result: SearchResult) -> float:
    """Calculate relevance score between query and search result (0-100).

    Scoring dimensions:
    - Brand/model matching: presence in title/URL/snippet
    - Keyword coverage: query term coverage rate
    - Title priority: title matches weighted higher than snippet
    - PDF documents: bonus for tender/price queries
    """
    score = 0.0
    haystack_title = f"{result.title} {result.url}".lower()
    snippet = (result.snippet or "").strip()
    haystack_full = f"{result.title} {snippet} {result.url}".lower() if snippet else haystack_title

    brand = (query.brand or "").strip().lower()
    model = (query.model or "").strip().lower()

    brand_score = 0.0
    model_score = 0.0

    if brand:
        if brand in haystack_title:
            brand_score = 25
        elif brand in haystack_full:
            brand_score = 12

    if model:
        model_normalized = re.sub(r'[-\s]+', '', model)
        title_normalized = re.sub(r'[-\s]+', '', haystack_title)
        full_normalized = re.sub(r'[-\s]+', '', haystack_full)

        if model_normalized in title_normalized or model in haystack_title:
            model_score = 35
        elif model_normalized in full_normalized or model in haystack_full:
            model_score = 18
        else:
            model_parts = [p for p in re.split(r'[-\s]+', model) if len(p) > 2]
            if model_parts:
                title_matches = sum(1 for part in model_parts if part.lower() in haystack_title)
                full_matches = sum(1 for part in model_parts if part.lower() in haystack_full)
                if title_matches >= len(model_parts) * 0.6:
                    model_score = 25
                elif full_matches >= len(model_parts) * 0.6:
                    model_score = 15

    # 防止品牌型号双重计分：如果型号包含品牌，品牌分减半
    if brand and model and brand in model.lower():
        brand_score *= 0.5

    score = brand_score + model_score

    query_text = query.text.lower()
    query_text = re.sub(r'["\'""]', ' ', query_text)
    query_text = re.sub(r'\b(?:site|filetype|inurl|intitle|official|specifications?|price|tender|award|procurement)\b', ' ', query_text, flags=re.I)
    query_terms = [term for term in query_text.split() if len(term) > 1]

    if query_terms:
        matched_terms = sum(1 for term in query_terms if term in haystack_full)
        coverage = matched_terms / len(query_terms)
        score += coverage * 20

    if query.purpose.lower() in ("price", "tender", "procurement", "award"):
        tender_markers = ("招标", "中标", "成交", "采购结果", "采购公告", "公告", "评标", "开标",
                         "tender", "award", "procurement", "contract award", "bid notice")
        if any(marker in haystack_full for marker in tender_markers):
            score += 5

        if result.url.lower().endswith('.pdf'):
            score += 10

    return min(score, 100.0)

@dataclass
class SearchRun:
    query: SearchQuery
    results: list[SearchResult]
    sources: list[SourceRecord]
    warnings: list[str]
    #: 逐条来源的线索对齐判定（`url -> AlignmentVerdict`）；不传线索时为空。
    alignments: dict[str, AlignmentVerdict] = field(default_factory=dict)

class FakeSearchProvider:
    name = "fake"
    def __init__(self, results=None, bodies=None, failures=None): self.results, self.bodies, self.failures = results or {}, bodies or {}, failures or {}
    def search(self, query):
        if query.text in self.failures: raise self.failures[query.text]
        return list(self.results.get(query.text, []))[:query.limit]
    def fetch(self, url, timeout):
        value = self.bodies.get(url)
        if isinstance(value, BaseException): raise value
        if value is None: raise IOError("body not found in fixture")
        return value


def _channel_list(names) -> str:
    """通道名写成代号里的一段（逗号分隔，顺序按第一次出现）。"""
    unique = [str(name) for name in names if str(name or '').strip()]
    return ','.join(dict.fromkeys(unique))


def empty_result_warnings(empty_channels, failed_channels) -> list[str]:
    """一次 ``SearchRun`` 一条结果都没有时，对外到底该说哪一句。

    内部事实是"**哪一个通道**这一次没有结果"：研究链路每次只传一个通道
    （``procurement_research`` 的发现/价格链路），交互式链路的 ``SearchRun`` 才一次传
    多个。所以这里按内部粒度分三句，翻译层照抄这个粒度（``report_library``）：

    * ``empty_results:<通道名>``——只有部分（或一个）通道为空。**不许**读成整轮没有结果；
    * ``empty_results_all_channels:<通道名>``——传进来且跑通的通道**全部**为空，
      这才真是"整轮没有结果"；
    * ``search_channel_failed:<通道名>``——有通道抛异常。失败与"确实没有结果"是两件事
      （：一次通道失败不许放大成整条链路失败，也不许说成"没有查到"）。
      它刻意**不用** ``provider_failed:`` 前缀：那个前缀在研究链路会被整条过滤掉
      （丢给排查用的 ``failures``），对外就会连"某通道这次没跑通"都不剩。
    """
    empty = [str(name) for name in dict.fromkeys(empty_channels) if str(name or '').strip()]
    failed = [str(name) for name in dict.fromkeys(failed_channels) if str(name or '').strip()]
    out: list[str] = []
    if empty:
        names = _channel_list(empty)
        if len(empty) > 1 and not failed:
            out.append(f'empty_results_all_channels:{names}')
        else:
            out.append(f'empty_results:{names}')
    if failed:
        out.append(f'search_channel_failed:{_channel_list(failed)}')
    if not out:
        # 没有通道可判：调用方直接给了一份空结果集（``results=[]``），或者所有通道都被
        # ``supports`` 跳过（没有通道真的跑过）。这种时候保留旧代号，翻译层照旧说
        # 「这一次检索没有返回任何结果」——它不是"某个通道空"这个事实。
        out.append('empty_results')
    return out


class OfficialSearchService:
    def __init__(self, provider: SearchProvider | None = None, fetch_body: bool = True, timeout: float = 8.0, providers=None, reader=None):
        self.providers = list(providers or ([provider] if provider else [])); self.fetch_body, self.timeout = fetch_body, timeout
        self.reader = reader
        # One shared reader is not documented as thread-safe, and it is a
        # fallback rather than the common path; serialising it keeps parallel
        # body fetches from changing the reader's own behaviour.
        self._reader_lock = threading.Lock()
    def run(self, query: SearchQuery, lead=None, attempted_queries=(), windows=(), results=None,
            exclude_urls=None) -> SearchRun:
        # Validate the provider-facing query through the immutable hardened
        # contract while retaining the legacy query object expected by older
        # adapters and serialized API responses.
        from backend.domain.search_improved import SearchQuery as ImmutableSearchQuery
        hardened_query = ImmutableSearchQuery(
            text=query.text, candidate_id=query.candidate_id, brand=query.brand,
            model=query.model, limit=query.limit, purpose=query.purpose,
            # 线索对齐字段要透传给 provider（对话 2 把它们拼成
            # buyerName / projectId / start_time / end_time）；不透传的话
            # 查询计划生成了参数、通道却永远收不到。
            buyer_name=query.buyer_name, supplier_name=query.supplier_name,
            project_id=query.project_id, window_start=query.window_start, window_end=query.window_end,
        )
        # 一条线索 = 一次判定口径；`lead=None` 时整条链路与改动前完全一致。
        lead_profile = coerce_lead(lead) if lead is not None else None
        attempted = tuple(str(q) for q in attempted_queries if str(q).strip())
        spans = tuple((w[0], w[1]) for w in windows if len(tuple(w)) >= 2)

        warnings=[]
        raw=[]
        #: 这一次 run 里"跑通但没有返回"的通道，以及"抛异常"的通道。两者是两件事：
        #: 前者是"这个通道这一次没有结果"，后者是"这个通道这一次没跑通"。
        empty_channels: list[str] = []
        failed_channels: list[str] = []
        if results is not None:
            # 已经召回好的结果（例如百度兜底路径按主机筛过的那批）。这条入口
            # 只跳过"搜索"这一步，后面的分类、抓正文、对齐判定全部复用同一条链，
            # 否则兜底路径就会长出一套和主路径不一样的口径。
            raw.extend([SearchResult(x.url, x.title, x.snippet,
                                     getattr(x, "provider", None) or "unknown",
                                     x.retrieved_at) for x in results])
        else:
            for provider in self.providers:
                if hasattr(provider, "supports") and not provider.supports(hardened_query):
                    continue
                channel = str(getattr(provider, "name", "unknown") or "unknown")
                try:
                    found = list(provider.search(hardened_query))
                    raw.extend([SearchResult(x.url, x.title, x.snippet, x.provider if x.provider != "unknown" else channel, x.retrieved_at) for x in found])
                    if found and getattr(provider, "stop_on_results", False):
                        break
                    if not found:
                        empty_channels.append(channel)
                except Exception as exc:
                    failed_channels.append(channel)
                    warnings.append(f"provider_failed:{channel}: {exc}")
        dedup={}; provenance={}
        for r in raw:
            try: u=normalize_url(r.url)
            except ValueError as exc: warnings.append(str(exc)); continue
            name = r.provider or getattr(next((p for p in self.providers if getattr(p,'name',None)==r.provider), None), 'name', 'unknown')
            if u not in dedup: dedup[u]=SearchResult(u,r.title,r.snippet,name,r.retrieved_at); provenance[u]=[name]
            elif name not in provenance[u]: provenance[u].append(name)
        sources=[]
        # 预计算所有分类，避免重复计算
        classifications = {id(r): classify_source(r.url, r.title) for r in dedup.values()}

        def combined_score(result: SearchResult) -> float:
            st = classifications[id(result)]
            auth_score, _ = authority(st)
            rel_score = relevance_score(hardened_query, result)
            if lead_profile is None:
                return auth_score * 0.6 + rel_score * 0.4
            # 「线索吻合度」必须进排序：否则杭锦旗那类公告
            # （.gov.cn、authority_score=85、与目标型号无关）会靠权威分排到前面。
            # 这一层只看标题/摘要/URL——正文这时候还没抓。
            field_match = lead_text_match(lead_profile, f"{result.title} {result.snippet} {result.url}")
            return auth_score * 0.4 + rel_score * 0.2 + field_match * 100 * 0.4

        ordered = sorted(dedup.values(), key=combined_score, reverse=True)[:query.limit]
        if exclude_urls:
            # 同一条线索的前几步可能召回同一篇公告（真机实测：案例③ 第 1、2 步都
            # 命中同一篇 ccgp 原文）。抓过的正文不必再抓一次——正文也是真请求。
            excluded = {str(u).strip().casefold() for u in exclude_urls}
            ordered = [r for r in ordered if r.url.strip().casefold() not in excluded]
        # Bodies are fetched before the sources are assembled. Fetching is the
        # only part of this loop that waits on the network, it is per-URL
        # independent, and doing it one URL at a time was the single longest
        # wait in a research round. The parallelism is bounded by a hard ceiling
        # (``CCGP_BODY_FETCH_WORKERS``) and, on the CCGP side, still serialised
        # by that adapter's own body clock, so this does not turn into a burst.
        bodies = self._fetch_bodies(ordered, classifications, hardened_query)
        for r in ordered:
            st = classifications[id(r)]
            score, reason = authority(st)
            level = PriceEvidenceLevel.DEALER_LEAD if st in (SourceType.DEALER, SourceType.AGGREGATOR) else (PriceEvidenceLevel.OFFICIAL_PUBLIC if st in (SourceType.MANUFACTURER_WEBSITE, SourceType.MANUFACTURER_DOWNLOAD) else PriceEvidenceLevel.UNKNOWN)
            s=SourceRecord(r.url,r.title,st,score,reason,r.snippet,provider=r.provider,provider_provenance=provenance[r.url],price_evidence_level=level,retrieved_at=r.retrieved_at)
            if self.fetch_body and st != SourceType.SEARCH_SNIPPET:
                body, fetch_error = bodies.get(r.url, (None, None))
                if isinstance(body, str) and body.strip():
                    s.body_text = body
                    s.body_status=BodyStatus.FETCHED; s.crawled_at=datetime.now(timezone.utc)
                elif isinstance(fetch_error, TimeoutError):
                    s.body_status=BodyStatus.TIMEOUT; s.error_reason=str(fetch_error); warnings.append(f"body_timeout: {r.url}")
                else:
                    s.body_status=BodyStatus.FAILED; s.error_reason=str(fetch_error or "no body fetch provider"); warnings.append(f"body_failed: {r.url}")
            if st is SourceType.TENDER_DOCUMENT:
                if _tender_body_verified(s):
                    s.price_evidence_level = PriceEvidenceLevel.HISTORICAL_TENDER
                    _extract_tender_metadata(s)
                else:
                    warnings.append(f"tender_body_unverified: {r.url}")
            sources.append(s)
        if not raw:
            # 说清是哪个通道：内部是"某一个通道这一次没有结果"，对外就不能说成
            # "整轮检索没有结果"。通道抛异常时另写失败句。
            warnings.extend(empty_result_warnings(empty_channels, failed_channels))
        if sources and all(s.source_type not in (SourceType.MANUFACTURER_WEBSITE, SourceType.MANUFACTURER_DOWNLOAD, SourceType.TENDER_DOCUMENT) for s in sources):
            warnings.append("no_official_source")
        alignments: dict[str, AlignmentVerdict] = {}
        if lead_profile is not None:
            for s in sources:
                verdict = verify_against_lead(s, lead_profile, attempted_queries=attempted, windows=spans)
                alignments[s.url] = verdict
                if verdict.state == STATE_REJECTED:
                    warnings.append(f"lead_rejected:{s.url}")
                elif verdict.state == STATE_UNRESOLVED:
                    warnings.append(f"lead_unresolved:{s.url}")
        return SearchRun(query,ordered,sources,warnings,alignments)

    def _fetch_bodies(self, ordered, classifications, hardened_query) -> dict[str, tuple[str | None, Exception | None]]:
        """Fetch every ordered body, bounded and in parallel.

        Returns ``{url: (body, error)}`` and never raises: one dead page must
        not take the round down, which is the behaviour the serial loop had.
        ``SEARCH_SNIPPET`` results carry no page to read, and URLs the caller
        excluded were removed before this point.

        The worker count is capped by ``CCGP_BODY_FETCH_WORKERS``. On the CCGP
        side the adapter still serialises the *starts* of these requests on its
        own body clock, so a wider pool cannot become a faster burst -- it only
        stops slow pages from being waited on one after another.
        """
        targets = [r for r in ordered
                   if self.fetch_body and classifications[id(r)] != SourceType.SEARCH_SNIPPET]
        if not targets:
            return {}
        workers = max(1, min(int(CCGP_BODY_FETCH_WORKERS), len(targets)))
        fetched: dict[str, tuple[str | None, Exception | None]] = {}
        if workers == 1:
            for result in targets:
                fetched[result.url] = self._fetch_body_outcome(result, hardened_query)
            return fetched
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_body_outcome, result, hardened_query): result
                       for result in targets}
            for future in as_completed(futures):
                result = futures[future]
                fetched[result.url] = future.result()
        return fetched

    def _fetch_body_outcome(self, result, hardened_query) -> tuple[str | None, Exception | None]:
        """One body read as a value, so a worker thread never raises into the pool."""
        try:
            return self._fetch_body(result, hardened_query), None
        except Exception as exc:
            return None, exc

    def _fetch_body(self, result, hardened_query) -> str:
        # Fetch with the provider that produced this result. Using the first
        # provider with ``fetch`` applied one provider's transport/decoder to
        # unrelated sites and caused failures whenever the result source
        # changed.
        fetcher = next((p for p in self.providers
                        if getattr(p, 'name', None) == result.provider and hasattr(p, 'fetch')), None)
        if fetcher is None:
            # Discovery-only providers (model web search, Tavily) return URL
            # citations and own no transport. The runtime keeps a fetch-only
            # client for exactly this case and marks it by refusing to support
            # any query; falling back to the shared reader instead would put a
            # real network read in front of an injected policy.
            fetcher = next((p for p in self.providers
                            if hasattr(p, 'fetch') and not _supports(p, hardened_query)), None)
        if fetcher is None:
            fetcher = next((p for p in self.providers if hasattr(p, 'fetch')), None)
        body = None
        fetch_error = None
        # Preserve an explicitly injected provider transport. The shared reader
        # is a fallback for HTTP/WAF failures, not a way to bypass caller/test
        # network policy.
        if fetcher is not None:
            try:
                body = fetcher.fetch(result.url, self.timeout)
            except Exception as exc:
                fetch_error = exc
        if (not isinstance(body, str) or not body.strip()) and self.reader is not None:
            # The shared page reader owns HTTP/browser fallback and its
            # provenance. A provider decoder remains available for binary
            # responses (notably PDF) whose reader text is intentionally empty.
            with self._reader_lock:
                page = self.reader.read(result.url, self.timeout)
            body = getattr(page, 'text', None) or ''
            if not body and fetcher is not None and hasattr(fetcher, 'decode_body'):
                body = fetcher.decode_body(page.payload, page.content_type, result.url)
        if not isinstance(body, str) or not body.strip():
            if fetch_error is not None:
                raise fetch_error
            raise IOError("no body fetch provider")
        # 落盘层的号码掩码：这是一条**独立车道**
        # （``POST /tasks/{id}/search`` → ``search_runs`` 表），正文不进
        # ``procurement_research.fetch_source``，所以共享掩码必须在这里也接一次，
        # 否则同一个洞里只堵了一半。掩在返回之前：下游的 ``price_quote``／
        # ``tender_title`` 等派生字段都从这份正文生成，先掩后派生才自洽。
        from backend.services.education_platform_sources import mask_landed_text
        return mask_landed_text(body)

def queries_from_candidate(candidate, purpose: str = "official") -> list[SearchQuery]:
    brand=getattr(candidate,"brand",None); model=getattr(candidate,"model",None)
    text=" ".join(x for x in (brand,model) if x)
    if not text.strip(): return []
    suffixes = {
        "tender": "中标 成交公告 型号 单价",
        "procurement": "中标 成交公告 型号 单价",
        "award": "中标 成交公告 型号 单价",
        "price": "官网 公开价格",
        "discovery": "同类产品 具体型号 技术参数",
    }
    suffix = suffixes.get(str(purpose).casefold(), "official specifications")
    return [SearchQuery(text=f'"{text}" {suffix}', candidate_id=getattr(candidate,"id",None), brand=brand, model=model, purpose=purpose)]


#: CCGP 的时间窗超过约 366 天会**静默返回 0 条**（实测），
#: 所以窗口一律切到 366 天以内，而不是交给通道的固定一年窗口。
MAX_PLAN_WINDOW_DAYS = 366
#: 宽窗口最多保留最近 3 段，避免查询式数量失控。
MAX_PLAN_WINDOW_SPANS = 3
#: 单条线索默认回溯一年（终点取线索日期/当天）。
LEAD_WINDOW_LOOKBACK_DAYS = 365


def _window_spans(start, end) -> tuple[tuple[str, str], ...]:
    """把 ``[start, end]`` 切成 ≤366 天的段；最近的排最前，最多 ``MAX_PLAN_WINDOW_SPANS`` 段。"""
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except (TypeError, ValueError):
        return ()
    if first > last:
        first, last = last, first
    spans = []
    cursor = first
    while cursor <= last:
        span_end = min(cursor + timedelta(days=MAX_PLAN_WINDOW_DAYS), last)
        spans.append((cursor.isoformat(), span_end.isoformat()))
        cursor = span_end + timedelta(days=1)
    return tuple(reversed(spans[-MAX_PLAN_WINDOW_SPANS:]))


def _lead_windows(lead) -> tuple[tuple[str, str], ...]:
    """查询窗口：优先用线索显式给的窗口，其次由线索日期往前一年推，最后退回今天。"""
    today = date.today()
    fallback = (((today - timedelta(days=LEAD_WINDOW_LOOKBACK_DAYS)).isoformat(), today.isoformat()),)
    if lead.window_start or lead.window_end:
        start = lead.window_start or lead.window_end
        end = lead.window_end or lead.window_start
        spans = _window_spans(start, end)
        return spans or fallback
    if lead.date:
        try:
            end = date.fromisoformat(lead.date)
        except ValueError:
            return fallback
        return _window_spans((end - timedelta(days=LEAD_WINDOW_LOOKBACK_DAYS)).isoformat(), end.isoformat()) or fallback
    return fallback


def _lead_plan_steps(lead) -> list[dict]:
    """按精度从高到低排的查询式（不含量时间窗），对应  对话 3 第 1 条。

    ``project_id`` 精确 → ``buyer_name`` + 品类词 → ``buyer_name`` + 型号 →
    ``supplier_name`` → ``buyer_name`` 单独 → 型号 + 品类词（兜底）。

    **每一步的 ``brand`` / ``model`` 是显式决定的，不是整条线索无条件继承**：
    CCGP 的 `query_ladder` 只要拿到 brand/model 就用它当关键词、把 `text` 丢掉
    （真机实测：案例③计划里 4 条 text 不同的查询，发出的 URL 有两组完全相同；
    案例② 的 ``kw=kcs2025-WT009`` 那一步实际发的是 ``kw=1500A2``）。
    所以只有"型号"那两步才带 model/brand，其余步骤让 `text` 自己当关键词——
    否则品类词、供应商名、买方名这三条视角在通道上从来不会真的被搜到。
    """
    steps: list[dict] = []

    def add(text: str, **extra) -> None:
        step = {"text": text, "brand": None, "model": None}
        step.update(extra)
        steps.append(step)

    if lead.project_id:
        add(lead.project_id, project_id=lead.project_id)
    if lead.buyer and lead.category:
        add(lead.category, buyer_name=lead.buyer)
    if lead.buyer and lead.model:
        add(lead.model, buyer_name=lead.buyer, model=lead.model)
    if lead.supplier:
        # CCGP 表单没有供应商字段，所以供应商名只能当 kw 用；
        # 结构化字段同时带上，将来通道支持时不必再改这里。
        add(lead.supplier, supplier_name=lead.supplier)
    if lead.buyer:
        add(lead.buyer, buyer_name=lead.buyer)
    fallback = " ".join(part for part in (lead.model, lead.category) if part)
    if fallback:
        add(fallback, brand=lead.brand, model=lead.model)
    return steps


def queries_from_lead(lead, limit: int = 10) -> list[SearchQuery]:
    """由一条线索派生查询计划；**每条查询式都带时间窗**（不再依赖固定一年）。

    线索可以是 `LeadProfile` / mapping / 任何带
    ``model`` / ``brand`` / ``supplier`` / ``buyer`` / ``category`` / ``date`` 的对象。
    没有 ``project_id`` 时从 ``buyer_name + 品类词`` 起算；一个检索键都没有时返回 ``[]``。
    """
    profile = coerce_lead(lead)
    steps = _lead_plan_steps(profile)
    if not steps:
        return []
    queries: list[SearchQuery] = []
    seen: set[tuple] = set()
    for span_start, span_end in _lead_windows(profile):
        for step in steps:
            key = (step["text"], step.get("buyer_name"), step.get("supplier_name"),
                   step.get("project_id"), step.get("brand"), step.get("model"),
                   span_start, span_end)
            if key in seen:
                continue
            seen.add(key)
            queries.append(SearchQuery(
                text=step["text"],
                candidate_id=profile.candidate_id,
                brand=step.get("brand"),
                model=step.get("model"),
                limit=limit,
                purpose="tender",
                buyer_name=step.get("buyer_name"),
                supplier_name=step.get("supplier_name"),
                project_id=step.get("project_id"),
                window_start=span_start,
                window_end=span_end,
            ))
    return queries


#: 百度一页 20 条（`rn=20`），与实测的页面形态一致。
BAIDU_QUERY_LIMIT = 20
#: 百度是"定位官方原文"的兜底，不是第二套召回：最多两条视角（买方视角 + 供应商视角）。
BAIDU_MAX_QUERIES = 2


def _baidu_keyword_combos(lead) -> list[str]:
    """线索字段拼成的百度关键词组合，最多 ``BAIDU_MAX_QUERIES`` 条。

    至少两个线索字段才拼——只给一个"生物安全柜"这样的品类词是泛搜，不是定位原文。
    串尾带"中标"，因为这条通道的用途就是找**已经成交**的那篇公告（②实测
    `库车市农业农村局 知楚 生物安全柜 中标` → 19 行、其中 gov/edu 15 条；
     同时记着字母数字型号在两个索引上都不如中文词有效）。
    """
    combos: list[str] = []

    def add(*parts):
        words = [str(part).strip() for part in parts if part and str(part).strip()]
        if len(words) < 2:
            return
        text = " ".join(words + ["中标"])
        if text not in combos and len(combos) < BAIDU_MAX_QUERIES:
            combos.append(text)

    add(lead.buyer, lead.brand, lead.category)
    add(lead.supplier, lead.brand, lead.model)
    add(lead.buyer, lead.model, lead.category)
    return combos


def baidu_queries_from_lead(lead, limit: int = BAIDU_QUERY_LIMIT) -> list[SearchQuery]:
    """把一条线索翻成百度关键词组合（用途只有"定位官方原文"）。

    结果 URL 只是**候选公告**：调用方必须按主机筛 `.gov.cn` / `.edu.cn` 再交给闸门，
    不许把 SERP 摘要当证据、更不许绕过 `lead_alignment` 直接进结论。
    百度没有时间窗参数，所以这些查询**不带窗口**——结论里也不许假装有（
    只允许零填充 ISO；宁可为空也不能编一个）。
    """
    profile = coerce_lead(lead)
    return [
        SearchQuery(text=text, candidate_id=profile.candidate_id, brand=profile.brand,
                    model=profile.model, limit=limit, purpose="tender")
        for text in _baidu_keyword_combos(profile)
    ]
