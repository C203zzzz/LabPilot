#!/usr/bin/env python
"""真机端到端：一条线索 → 查询计划 → CCGP/百度真请求 → 召回 → 闸门判定。

规矩（照 `scripts/ccgp_probe.py` 的纪律，少一条都不行）：

* **只走生产代码路径**：`lead_search.search_lead` + 产品自己的 `CCGPSearchProvider` /
  `BaiduSearchProvider`，脚本不自己拼 URL，也不自己判定。
* **6 秒节流是通道自带的**（`CCGPSearchProvider.min_interval`，模块级 pace），脚本不改它、
  不并发、不重试。命中站点限速页时通道会抛 `ProviderConfigurationError`，编排会停手，
  脚本打 STOP 并以退出码 2 结束 —— 不 sleep-and-continue，不换身份。
* **每一条请求都留证据**：URL、耗时、字节数、该次检索返回行数（`RequestLedger`），
  连同一行线索的最终判定一起写进 `--out` 下的 JSON 和 stdout。

用法：

    python scripts/lead_align_live_e2e.py --case both --out tmp/lead-align-live

真机跑一次会花掉十几到二十几个请求，其中 CCGP 每个请求至少间隔 6 秒；
`--dry-run` 只打印将要跑的计划（不发请求）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.infrastructure.search_providers import (  # noqa: E402
    BaiduSearchProvider,
    CCGPSearchProvider,
)
from backend.services.lead_alignment import LeadProfile  # noqa: E402
from backend.services.lead_search import (  # noqa: E402
    DEFAULT_REQUEST_BUDGET,
    baidu_queries_from_lead,
    search_lead,
)
from backend.services.official_search import queries_from_lead  # noqa: E402

#: 设计规范 的案例②和案例③。线索字段按工具真实拿到的形态填：
#: 案例②的项目编号/卖方/品类来自公告本身，案例③的买方/品牌/型号来自聚合站线索。
CASES: dict[str, LeadProfile] = {
    "kucha": LeadProfile(
        buyer="库车市农业农村局", supplier="新疆鑫华力科析科技有限公司", project_id="kcs2025-WT009",
        brand="知楚", model="1500A2", category="生物安全柜", date="2025-02-18",
    ),
    "hangjin": LeadProfile(
        buyer="杭锦旗疾病预防控制中心", brand="泰斯特", model="SPX-150BIII",
        category="生化培养箱", date="2026-09-21",
    ),
}


def print_plan(name: str, lead: LeadProfile) -> None:
    print(f"# {name} 线索：{lead}")
    for index, query in enumerate(queries_from_lead(lead), 1):
        print(f"  ccgp  #{index} kw={query.text!r} buyer={query.buyer_name!r} "
              f"project={query.project_id!r} window={query.window_start}..{query.window_end}")
    for index, query in enumerate(baidu_queries_from_lead(lead), 1):
        print(f"  baidu #{index} wd={query.text!r}（无时间窗）")


def run_case(name: str, lead: LeadProfile, *, out_dir: Path, budget, timeout: float) -> dict:
    print(f"\n=== {name} ===")
    print_plan(name, lead)
    ccgp = CCGPSearchProvider(timeout=timeout)
    baidu = BaiduSearchProvider(timeout=timeout)
    started = time.time()
    outcome = search_lead(lead, ccgp_provider=ccgp, fallback_provider=baidu, budget=budget,
                          timeout=timeout)
    elapsed = time.time() - started

    print(f"\n-- 逐请求（{outcome.ledger.total} 个，共 {elapsed:.1f}s）--")
    for index, record in enumerate(outcome.ledger.records, 1):
        rows = "?" if record.rows is None else record.rows
        line = (f"[{index:02d}] {record.channel} {record.seconds:6.2f}s {record.payload_bytes:>9}B rows={rows}")
        if record.error:
            line += f"  ERROR {record.error}"
        print(line)
        print(f"     {record.url}")

    print("\n-- 逐次检索 --")
    for attempt in outcome.attempts:
        print(f"  {attempt.channel:5s} rows={attempt.rows:<3} candidates={attempt.candidates:<3} "
              f"text={attempt.text!r} window={attempt.window} {attempt.note}")

    print("\n-- 逐条判定 --")
    for verdict in outcome.verdicts:
        print(f"  {verdict.state:10s} score={verdict.combined_score:.3f} {verdict.url}")
        print(f"     理由：{verdict.reason}")

    print(f"\n-- 结论 --\nstate={outcome.state}  budget_exhausted={outcome.budget_exhausted}  "
          f"rate_limited={outcome.rate_limited}")
    print(f"reason={outcome.reason}")
    if outcome.skipped_queries:
        print(f"未执行：{outcome.skipped_queries}")
    print(f"账本：{json.dumps(outcome.ledger.summary(), ensure_ascii=False)}")

    payload = {"case": name, "lead": {k: v for k, v in lead.__dict__.items()},
               "elapsed_seconds": round(elapsed, 2), **outcome.to_dict()}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", default="both", choices=[*CASES, "both"])
    parser.add_argument("--out", default="tmp/lead-align-live")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--ccgp-budget", type=int, default=DEFAULT_REQUEST_BUDGET["ccgp"])
    parser.add_argument("--baidu-budget", type=int, default=DEFAULT_REQUEST_BUDGET["baidu"])
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    args = parser.parse_args(argv)

    names = list(CASES) if args.case == "both" else [args.case]
    if args.dry_run:
        for name in names:
            print_plan(name, CASES[name])
        return 0

    budget = {"ccgp": args.ccgp_budget, "baidu": args.baidu_budget}
    out_dir = Path(args.out)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"# 真机端到端 {started}  budget={budget}  timeout={args.timeout}s  "
          f"（CCGP 每请求 >= 6s 节流，撞限速即停）")

    payloads = []
    for name in names:
        try:
            payloads.append(run_case(name, CASES[name], out_dir=out_dir, budget=budget,
                                     timeout=args.timeout))
        except Exception as exc:  # 通道级失败（含限速）：如实报，不重试
            print(f"\n# STOP: {name} 抛出 {type(exc).__name__}: {exc}")
            return 2
        if payloads[-1]["rate_limited"]:
            print(f"\n# STOP: {name} 命中站点限速页，本次运行不再发任何请求")
            return 2

    (out_dir / "summary.json").write_text(
        json.dumps([{k: p[k] for k in ("case", "state", "reason", "elapsed_seconds", "ledger",
                                       "skipped_queries", "budget_exhausted", "rate_limited")}
                    for p in payloads], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n# run end {time.strftime('%Y-%m-%d %H:%M:%S')}  证据在 {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
