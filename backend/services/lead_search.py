"""线索对齐的检索编排：查询计划 → 通道 → 召回 → 闸门判定。

这个模块只做三件事，别往里塞第四件：

1. **按上限花请求。** 一条线索最多花多少个 HTTP 请求是显式参数（`RequestLedger`），
   不是"看计划跑多远"的结果。CCGP 有 6 秒硬节流，③ 记着三段窗口一次查询
   就要 18.16 秒；6 步计划 × 3 段最坏 54 秒纯等待，所以上限必须在这里落地。
   触到上限不是静默放弃：剩下的查询式进 `skipped_queries`，结论是 `unresolved`，
   理由里写明"已试哪些、哪些没试"。
2. **百度通道作为兜底路径。** CCGP 没给出 `verified` 时，用线索的品牌/型号/供应商/品类
   经 `BaiduSearchProvider` 去定位官方原文；结果**按主机筛 `.gov.cn` / `.edu.cn`**、
   丢掉目录页，然后把筛过的候选交给 `OfficialSearchService.run(results=…)` 走同一条
   分类/抓正文/对齐链。百度只负责"找到候选公告"，判定仍然是 `lead_alignment`。
3. **撞到限速立刻停手。** CCGP 返回它自己的"频繁访问"页时（`ProviderConfigurationError`），
   这一条线索的全部剩余请求都不再发（包括百度），并把 `rate_limited` 标在结论上。
   `OfficialSearchService.run` 会把 provider 异常收成 warning（那是它的既有语义），
   所以这里按 warning 文本识别，而不是假设异常会冒上来。
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from backend.domain.search import SearchResult
from backend.services.lead_alignment import (
    STATE_REJECTED,
    STATE_UNRESOLVED,
    STATE_VERIFIED,
    AlignmentVerdict,
    LeadProfile,
    coerce_lead,
)
from backend.services.official_search import (
    OfficialSearchService,
    baidu_queries_from_lead,
    queries_from_lead,
)

#: 官方发布方的主机后缀。百度 SERP 上"不是这两类"的一律不进闸门（② 里
#: 那 19 行有 4 行是 `qianlima.com`，聚合站正是要筛掉的那一类）。
GOV_EDU_SUFFIXES = (".gov.cn", ".edu.cn")

#: 每条线索的请求上限，按通道分。理由见报告 §上限怎么定的：
#: CCGP 8 次 × 6 秒硬节流 ≈ 48 秒上限，够"1–2 次检索 + 最多 4 篇正文"；
#: 百度无限速，5 次（1 次检索 + 最多 4 篇正文）几秒完成。
DEFAULT_REQUEST_BUDGET: Mapping[str, int] = {"ccgp": 8, "baidu": 5}

#: `OfficialSearchService.run` 把 provider 异常写成 ``provider_failed:<name>: <exc>``。
RATE_LIMIT_MARKERS = ("rate limited", "频繁访问", "访问过于频繁")
BUDGET_MARKER = "请求上限已用尽"


class RequestBudgetExhausted(RuntimeError):
    """本通道的请求上限已用尽：停下来并如实报告，不静默放弃。"""


@dataclass
class RequestRecord:
    """一次真实 HTTP 请求。真机端到端的逐请求证据就从这里来。"""

    channel: str
    url: str
    seconds: float
    payload_bytes: int
    error: str | None = None
    #: 这次 `search_once` 解析出的行数（只挂在该次调用的最后一条请求上）。
    rows: int | None = None

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "url": self.url,
            "seconds": round(self.seconds, 3),
            "payload_bytes": self.payload_bytes,
            "error": self.error,
            "rows": self.rows,
        }


@dataclass
class RequestLedger:
    """按通道计数的请求账本（搜索与正文抓取都算，两者都真的发 HTTP）。"""

    allowance: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_REQUEST_BUDGET))
    records: list[RequestRecord] = field(default_factory=list)

    def limit(self, channel: str) -> int | None:
        return self.allowance.get(channel)

    def spent(self, channel: str) -> int:
        return sum(1 for record in self.records if record.channel == channel)

    def remaining(self, channel: str) -> int | None:
        cap = self.limit(channel)
        return None if cap is None else max(0, cap - self.spent(channel))

    def exhausted(self, channel: str) -> bool:
        remaining = self.remaining(channel)
        return remaining is not None and remaining <= 0

    def ensure_budget(self, channel: str) -> None:
        """请求**之前**的预检：超限就抛，不把请求发出去。"""
        if self.exhausted(channel):
            raise RequestBudgetExhausted(
                f"{channel} {BUDGET_MARKER}（上限 {self.limit(channel)}，已花 {self.spent(channel)}）")

    def add_record(self, record: RequestRecord) -> None:
        self.records.append(record)

    @property
    def total(self) -> int:
        return len(self.records)

    def summary(self) -> dict:
        return {
            "total": self.total,
            "channels": {
                channel: {"limit": self.limit(channel), "spent": self.spent(channel)}
                for channel in sorted(set(self.allowance) | {r.channel for r in self.records})
            },
        }


def instrument_provider(provider: Any, ledger: RequestLedger) -> Any:
    """把 provider 的真实请求记进账本，并在超限时打断它。

    打的是实例的 `_request`（不是类），所以 6 秒节流、`network_url` 校验、
    限速暂停这些既有语义一个都不绕过；`search_once` 的返回值再补记"这次检索几行"。
    """
    channel = str(getattr(provider, "name", "unknown"))
    original_request = provider._request

    def counted_request(url, timeout):
        ledger.ensure_budget(channel)
        started = time.monotonic()
        try:
            payload, content_type = original_request(url, timeout)
        except BaseException as exc:
            ledger.add_record(RequestRecord(channel, str(url), time.monotonic() - started, 0,
                                            f"{type(exc).__name__}: {exc}"))
            raise
        ledger.add_record(RequestRecord(channel, str(url), time.monotonic() - started, len(payload)))
        return payload, content_type

    provider._request = counted_request

    original_once = getattr(provider, "search_once", None)
    if callable(original_once):
        def counted_search_once(query, _original=original_once):
            before = len(ledger.records)
            rows = _original(query)
            for record in reversed(ledger.records[before:]):
                if record.rows is None:
                    record.rows = len(rows)
                    break
            return rows

        provider.search_once = counted_search_once
    return provider


def is_gov_or_edu(url: Any) -> bool:
    """主机是否 `.gov.cn` / `.edu.cn`（百度结果进闸门之前的第一道筛）。"""
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host.endswith(GOV_EDU_SUFFIXES)


def is_directory_like(url: Any) -> bool:
    """路径以 `/` 结尾的栏目页/目录页不是一篇公告。

    ② 实测百度结果里有 `https://www.xjkc.gov.cn/zwgk/csxx/cjgg/` 这种栏目页；
    `index.htm` / `index_4.htm` 这类列表页同理（真机实测案例③ 有一条
    `http://www.ccgp.gov.cn/cggg/dfgg/zbgg/index_4.htm` 被当成候选抓了正文）。
    只按域名筛它们会混进闸门。这里丢掉，剩下的可疑页面交给闸门（没有型号/供应商证据
    的页面自然落到 `unresolved`，不会污染结论）。
    """
    path = urlsplit(str(url or "")).path
    if not path or path.endswith("/"):
        return True
    return bool(re.search(r"(^|/)index(_\d+)?\.(s?html?|jsp|php|aspx?)$", path, re.IGNORECASE))


def filter_official_candidates(results: Iterable[Any]) -> list[Any]:
    """百度结果 → 只留官方主机的单篇候选（按 URL 去重，保序）。"""
    kept: list[Any] = []
    seen: set[str] = set()
    for result in results or []:
        url = str(getattr(result, "url", "") or "")
        key = url.strip().casefold()
        if not key or key in seen:
            continue
        if not is_gov_or_edu(url) or is_directory_like(url):
            continue
        seen.add(key)
        kept.append(result)
    return kept


@dataclass
class AttemptRecord:
    """一次真的发出去的检索（不是计划里的一条）。"""

    channel: str
    text: str
    rows: int
    window: tuple[str | None, str | None] = (None, None)
    buyer_name: str | None = None
    project_id: str | None = None
    candidates: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "text": self.text,
            "rows": self.rows,
            "window": list(self.window),
            "buyer_name": self.buyer_name,
            "project_id": self.project_id,
            "candidates": self.candidates,
            "note": self.note,
        }


@dataclass
class LeadSearchOutcome:
    """一条线索跑完检索后的结论：三态 + 逐条判定 + 花掉的请求 + 没跑到的查询式。"""

    lead: LeadProfile
    state: str
    reason: str
    verdicts: list[AlignmentVerdict] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    skipped_queries: list[str] = field(default_factory=list)
    ledger: RequestLedger = field(default_factory=RequestLedger)
    budget_exhausted: bool = False
    rate_limited: bool = False
    warnings: list[str] = field(default_factory=list)

    def by_state(self, state: str) -> list[AlignmentVerdict]:
        return [v for v in self.verdicts if v.state == state]

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "budget_exhausted": self.budget_exhausted,
            "rate_limited": self.rate_limited,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "attempts": [a.to_dict() for a in self.attempts],
            "skipped_queries": list(self.skipped_queries),
            "ledger": self.ledger.summary(),
            "requests": [r.to_dict() for r in self.ledger.records],
            "warnings": list(self.warnings),
        }


def _rate_limited(warnings: Sequence[str]) -> bool:
    return any("provider_failed:" in w and any(m in w for m in RATE_LIMIT_MARKERS) for w in warnings)


def _budget_hit(warnings: Sequence[str]) -> bool:
    return any("provider_failed:" in w and BUDGET_MARKER in w for w in warnings)


def _absorb(verdicts: dict[str, AlignmentVerdict], run) -> None:
    for url, verdict in getattr(run, "alignments", {}).items():
        verdicts.setdefault(url, verdict)


def _conclude(profile: LeadProfile, verdicts: dict[str, AlignmentVerdict],
              attempts: Sequence[AttemptRecord], skipped: Sequence[str],
              budget_exhausted: bool, rate_limited: bool) -> tuple[str, str]:
    """整体三态与理由。硬结论优先，但"因上限没跑完"必须写进理由。"""
    verified = [v for v in verdicts.values() if v.state == STATE_VERIFIED]
    rejected = [v for v in verdicts.values() if v.state == STATE_REJECTED]
    unresolved = [v for v in verdicts.values() if v.state == STATE_UNRESOLVED]
    tried = "、".join(f"{a.channel}:{a.text}" for a in attempts) or "（一个请求都没发出去）"
    windows = "、".join(
        f"{a.text}[{a.window[0] or '?'}..{a.window[1] or '?'}]" for a in attempts) or "（无）"
    notes: list[str] = []
    if verified:
        state = STATE_VERIFIED
        for verdict in verified:
            notes.append(f"命中官方原文：{verdict.url}；{verdict.reason}")
    elif rejected:
        state = STATE_REJECTED
        notes.append(f"{len(rejected)} 条候选被闸门拒绝（型号/供应商硬冲突），"
                     f"首条 {rejected[0].url}：{rejected[0].reason}")
    else:
        state = STATE_UNRESOLVED
        notes.append(f"证据不足：{len(unresolved)} 条候选都没有可确定性信号")
    if budget_exhausted:
        notes.append(f"请求上限已用尽，以下查询式未执行：{'、'.join(skipped) or '（无）'}"
                     "——结论可能不完整，不是'搜过了没有'")
    if rate_limited:
        notes.append("站点返回限速页，本次检索已停手（未重试、未换身份）")
    if state == STATE_UNRESOLVED or budget_exhausted or rate_limited:
        notes.append(f"已试查询式：{tried}；时间窗：{windows}")
    return state, "；".join(notes)


def default_lead_providers(timeout: float | None = None):
    """这条路径默认用的两个通道（CCGP 主检索 + 百度定位原文）。

    刻意**不**注册进 `routes.py` / `main.py` 的通用 provider 链：百度
    `supports()` 恒为 True，一旦进通用链，每条老查询都会多打 1–3 个百度请求，
    而它的结果只能当候选、不能当证据。它属于对齐链，不属于通用检索链。
    """
    from backend.infrastructure.search_providers import BaiduSearchProvider, CCGPSearchProvider
    if timeout is None:
        return CCGPSearchProvider(), BaiduSearchProvider()
    return CCGPSearchProvider(timeout=timeout), BaiduSearchProvider(timeout=timeout)


@contextmanager
def _decoded_tables_only():
    """让正文抓取走本地的 `decode_body`，而不是 Jina Reader。

    为什么非这样不可：闸门判定"公告里写的是别的型号"靠的是表格行
    （`a | b | c`， 案例三那三行福玛就是表格）。`PublicWebSearchProvider.fetch`
    默认先试 Jina Reader，返回的是它自己清洗过的纯文本——表格结构会丢，闸门就
    抽不出"公告内是…"，案例三会退化成 `unresolved` 而不是 `rejected`。
    关掉它以后正文由本地 `_request` + `decode_body` 取，形态与夹具、与线上实测
    实测一致，而且正文抓取也真的计入请求账本并受 6 秒节流约束。

    恢复原值：这是调用级配置，不留在进程里给别人踩。
    """
    key = "ENABLE_JINA_READER"
    previous = os.environ.get(key)
    os.environ[key] = "false"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def search_lead(
    lead: Any,
    *,
    ccgp_provider: Any = None,
    fallback_provider: Any = None,
    budget: Mapping[str, int] | None = None,
    candidate_limit: int = 4,
    fetch_body: bool = True,
    timeout: float = 20.0,
    include_fallback: bool = True,
    instrument: bool = True,
    decoded_tables: bool = True,
) -> LeadSearchOutcome:
    """一条线索跑完整链路：CCGP 查询计划 → （必要时）百度兜底 → 闸门判定。

    `candidate_limit` 是"每条查询最多几个候选进闸门"，它管的是**正文抓取**的次数：
    正文也是真请求，也要占预算，不然 10 条候选会把预算全吃在抓取上。
    `decoded_tables` 见 `_decoded_tables_only`：闸门的表格行判据要求正文是本地
    `decode_body` 的形态，所以默认关掉 Jina Reader。
    `instrument=False` 只给离线调试用：账本会是空的，请求上限也就不会生效
    （预算靠账本计数），调用方必须自己控制请求量。
    """
    if decoded_tables:
        with _decoded_tables_only():
            return _search_lead(lead, ccgp_provider=ccgp_provider, fallback_provider=fallback_provider,
                                budget=budget, candidate_limit=candidate_limit, fetch_body=fetch_body,
                                timeout=timeout, include_fallback=include_fallback, instrument=instrument)
    return _search_lead(lead, ccgp_provider=ccgp_provider, fallback_provider=fallback_provider,
                        budget=budget, candidate_limit=candidate_limit, fetch_body=fetch_body,
                        timeout=timeout, include_fallback=include_fallback, instrument=instrument)


def _search_lead(
    lead: Any,
    *,
    ccgp_provider: Any = None,
    fallback_provider: Any = None,
    budget: Mapping[str, int] | None = None,
    candidate_limit: int = 4,
    fetch_body: bool = True,
    timeout: float = 20.0,
    include_fallback: bool = True,
    instrument: bool = True,
) -> LeadSearchOutcome:
    profile = coerce_lead(lead)
    ledger = RequestLedger(allowance=dict(budget or DEFAULT_REQUEST_BUDGET))
    if ccgp_provider is None or (fallback_provider is None and include_fallback):
        default_ccgp, default_baidu = default_lead_providers(timeout=timeout)
        if ccgp_provider is None:
            ccgp_provider = default_ccgp
        if fallback_provider is None and include_fallback:
            fallback_provider = default_baidu
    if instrument:
        instrument_provider(ccgp_provider, ledger)
        if fallback_provider is not None:
            instrument_provider(fallback_provider, ledger)

    ccgp_channel = str(getattr(ccgp_provider, "name", "ccgp"))
    service = OfficialSearchService(providers=[ccgp_provider], fetch_body=fetch_body, timeout=timeout)

    verdicts: dict[str, AlignmentVerdict] = {}
    attempts: list[AttemptRecord] = []
    skipped: list[str] = []
    warnings: list[str] = []
    attempted_texts: list[str] = []
    attempted_windows: list[tuple[str | None, str | None]] = []
    seen_signatures: set[tuple] = set()
    seen_urls: set[str] = set()
    budget_exhausted = False
    rate_limited = False

    for query in queries_from_lead(profile):
        # 通道真正看得见的字段才是"这一步和上一步不同"的依据：CCGP 的 kw 由
        # brand/model/text 里的 ladder 决定，两个 text 不同但通道参数相同的查询
        # 会发出同一批 URL（真机实测，见 `_lead_plan_steps` 的注释）。
        signature = (query.text, query.buyer_name, query.supplier_name, query.project_id,
                     query.brand, query.model, query.window_start, query.window_end)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        if ledger.exhausted(ccgp_channel):
            skipped.append(f"{ccgp_channel}:{query.text}")
            budget_exhausted = True
            continue
        effective = replace(query, limit=min(query.limit, candidate_limit)) if candidate_limit else query
        attempted_texts.append(effective.text)
        attempted_windows.append((effective.window_start, effective.window_end))
        run = service.run(effective, lead=profile, attempted_queries=attempted_texts,
                          windows=attempted_windows, exclude_urls=sorted(seen_urls))
        warnings.extend(run.warnings)
        attempts.append(AttemptRecord(
            channel=ccgp_channel, text=effective.text, rows=len(run.results),
            window=(effective.window_start, effective.window_end),
            buyer_name=effective.buyer_name, project_id=effective.project_id,
            candidates=len(run.sources),
            note="; ".join(w for w in run.warnings if w.startswith("provider_failed")) or
                 ("body_failed" if any(w.startswith("body_") for w in run.warnings) else ""),
        ))
        seen_urls.update(r.url.casefold() for r in run.sources)
        _absorb(verdicts, run)
        if _rate_limited(run.warnings):
            rate_limited = True
            break
        if _budget_hit(run.warnings):
            budget_exhausted = True
        if any(v.state == STATE_VERIFIED for v in verdicts.values()):
            break

    if (include_fallback and fallback_provider is not None and not rate_limited
            and not any(v.state == STATE_VERIFIED for v in verdicts.values())):
        fallback_channel = str(getattr(fallback_provider, "name", "fallback"))
        fallback_service = OfficialSearchService(providers=[fallback_provider], fetch_body=fetch_body,
                                                 timeout=timeout)
        for query in baidu_queries_from_lead(profile):
            if ledger.exhausted(fallback_channel):
                skipped.append(f"{fallback_channel}:{query.text}")
                budget_exhausted = True
                continue
            attempted_texts.append(query.text)
            attempted_windows.append((None, None))
            try:
                found = list(fallback_provider.search_once(query))
            except Exception as exc:  # 挑战页/超时：这条兜底路径失败要如实记，不重试
                warnings.append(f"provider_failed:{fallback_channel}: {exc}")
                if any(m in str(exc) for m in RATE_LIMIT_MARKERS):
                    rate_limited = True
                attempts.append(AttemptRecord(
                    channel=fallback_channel, text=query.text, rows=0,
                    note=f"failed: {type(exc).__name__}: {exc}"))
                break
            candidates = [r for r in filter_official_candidates(found)
                          if str(r.url).casefold() not in seen_urls]
            if not candidates:
                attempts.append(AttemptRecord(
                    channel=fallback_channel, text=query.text, rows=len(found),
                    note=f"官方主机筛选 + 去重后 0 条（原始 {len(found)}）"))
                continue
            effective = replace(query, limit=min(query.limit, candidate_limit)) if candidate_limit else query
            run = fallback_service.run(effective, lead=profile, attempted_queries=attempted_texts,
                                       windows=attempted_windows, results=candidates[:effective.limit])
            warnings.extend(run.warnings)
            attempts.append(AttemptRecord(
                channel=fallback_channel, text=query.text, rows=len(found),
                candidates=len(run.sources),
                note=f"官方主机候选 {len(candidates)} 条",
            ))
            seen_urls.update(r.url.casefold() for r in run.sources)
            _absorb(verdicts, run)
            if _rate_limited(run.warnings):
                rate_limited = True
                break
            if any(v.state == STATE_VERIFIED for v in verdicts.values()):
                break

    if not attempts and not rate_limited:
        warnings.append("lead_plan_empty: 线索没有可用检索键，一个请求都没发")
    state, reason = _conclude(profile, verdicts, attempts, skipped, budget_exhausted, rate_limited)
    return LeadSearchOutcome(
        lead=profile, state=state, reason=reason, verdicts=list(verdicts.values()),
        attempts=attempts, skipped_queries=skipped, ledger=ledger,
        budget_exhausted=budget_exhausted, rate_limited=rate_limited, warnings=warnings,
    )
