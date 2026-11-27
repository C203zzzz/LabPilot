"""第二步：抓取小零易购的结果公示（列表 + 逐单详情），**边抓边落盘**、可断点续抓。

用法（仓库根目录）：:

    # 先看探测结果，定计划
    .venv\\Scripts\\python.exe scripts\\xlyg_crawl.py plan
    # 抓列表（每个品类词按时间窗翻页）
    .venv\\Scripts\\python.exe scripts\\xlyg_crawl.py list
    # 逐单抓详情（串行 1.05 秒/页，支持 Ctrl-C 后续抓）
    .venv\\Scripts\\python.exe scripts\\xlyg_crawl.py detail

落盘位置（都在 ``output/xlyg-db/``，被 .gitignore 忽略）：

* ``list_rows.jsonl``   列表行，一行一条，追加写；同一个单被多个词搜到就会有多行
* ``details/<guid>.html``  **已脱敏**的详情页；闸门有命中就不写，改记 ``detail_gaps.jsonl``

低频：所有请求走产品自己的 ``request()``，同站串行且间隔 ≥1.05 秒。
不登录、不注册、不绕过任何访问控制；遇到验证码／封禁就停下如实记「被挡」。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.education_platform_sources import (  # noqa: E402
    GuangdongXiaolingyigouPlatform, request)
from backend.services.xlyg_local_db import (  # noqa: E402
    PROBE_KEYWORDS, detail_template, detail_url, is_terminal_state, list_window,
    normalize_deal_time, page_totals, parse_list_page, redact)

OUT = ROOT / 'output/xlyg-db'
DETAILS = OUT / 'details'
LIST_ROWS = OUT / 'list_rows.jsonl'
DETAIL_GAPS = OUT / 'detail_gaps.jsonl'
DONE_FILE = OUT / 'list_done.json'
SWEEP_PROGRESS = OUT / 'sweep_progress.json'


def _append(path, records):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def _read_jsonl(path):
    if not path.exists():
        return []
    records = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def read_list_rows():
    return _read_jsonl(LIST_ROWS)


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------

def list_request(keyword, window, platform=None):
    platform = platform or GuangdongXiaolingyigouPlatform()
    data = {**platform._form(platform._FIND_TARGET, ''),
            platform.form_keyword_param: keyword,
            'ctl00$ContentPlaceHolder1$txtStateDate': window[0],
            'ctl00$ContentPlaceHolder1$endStateDate': window[1]}
    return platform.list_url, 'POST', data


def page_fields(keyword, window, page, platform):
    """翻页要回发的整份表单：**筛选字段一个都不能丢**（见 ``fetch_list`` 的注释）。"""
    return {**platform._form(platform._PAGE_TARGET, str(page)),
            platform.form_keyword_param: keyword,
            'ctl00$ContentPlaceHolder1$txtStateDate': window[0],
            'ctl00$ContentPlaceHolder1$endStateDate': window[1]}


def fetch_list(keyword, window, max_pages, platform=None, log=print, sink=None):
    """一个品类词的全部列表页，**每页立刻交给 ``sink`` 落盘**（断点续抓）。

    ⚠️ **每一页都要把筛选字段一起回发**：只发 ``__doPostBack`` 的
    ``__EVENTTARGET``/``__EVENTARGUMENT``（产品 ``next_request`` 的写法）会丢掉项目名
    与时间窗筛选，站点把**整站未筛选列表**发回来——实测第 2 页的"记录总数"从
    几十条变成 2 万多条，翻页就翻到别的单上去了。
    """
    platform = platform or GuangdongXiaolingyigouPlatform()
    url, method, data = list_request(keyword, window, platform)
    html = request(url, method=method, data=data).decode('utf-8', 'replace')
    rows, pages = parse_list_page(html)
    totals = page_totals(html)
    total_pages = min(pages, max_pages)
    log(f'  {keyword}: 记录总数 {totals["records"]}、总页数 {pages}（本次取前 {total_pages} 页），'
        f'第 1 页 {len(rows)} 行')
    accepted = _tagged(rows, keyword)
    if sink:
        sink(accepted)
    previous = [row['bill_guid'] for row in rows]
    for page in range(2, total_pages + 1):
        if len(rows) < 10:
            # 上一页不满 10 行 = 已经是最后一页。
            # 站点印的「记录总数」有时比翻页实际给到的行数多（实测：离心机 84 个月
            # 窗口印 290 条，翻到第 25 页只有 7 行就没了，实取 247 条），**以实取为准**。
            log(f'    第 {page - 1} 页只有 {len(rows)} 行，到此为止（站点报 {totals["records"]} 条）')
            break
        fields = page_fields(keyword, window, page, platform)
        html = request(platform.list_url, method='POST', data=fields).decode('utf-8', 'replace')
        rows, _ = parse_list_page(html)
        if not rows:
            log(f'    第 {page} 页没有行，停止翻页')
            break
        guids = [row['bill_guid'] for row in rows]
        if guids == previous:
            # 越界页被站点用末页顶回来：再翻也是同一页，停。
            log(f'    第 {page} 页与上一页完全相同，停止翻页')
            break
        previous = guids
        page_tagged = _tagged(rows, keyword)
        accepted.extend(page_tagged)
        if sink:
            sink(page_tagged)
    return accepted


def _tagged(rows, keyword):
    stamp = datetime.now().isoformat(timespec='seconds')
    return [{**row, 'keyword': keyword, 'crawled_at': stamp} for row in rows]


def command_list(args):
    window = list_window(date.fromisoformat(args.today) if args.today else date.today(),
                         args.window_months)
    keywords = args.keywords or list(PROBE_KEYWORDS)
    done = set(json.loads(DONE_FILE.read_text(encoding='utf-8'))) if DONE_FILE.exists() else set()
    if args.redo:
        done = set()
    print(f'时间窗 {window[0]} ~ {window[1]}；品类词 {len(keywords)} 个（已完成 {len(done)}）；'
          f'每词最多 {args.max_pages} 页')
    seen = {row['bill_guid'] for row in read_list_rows()}
    print(f'已有列表行 {len(read_list_rows())} 行（{len(seen)} 个单），继续追加')
    for index, keyword in enumerate(keywords, 1):
        if keyword in done:
            print(f'[{index}/{len(keywords)}]   {keyword}: 已完成，跳过（--redo 可重抓）', flush=True)
            continue
        try:
            rows = fetch_list(keyword, window, args.max_pages, sink=lambda part: _append(LIST_ROWS, part),
                              log=lambda text: print(f'[{index}/{len(keywords)}]' + text, flush=True))
        except Exception as exc:
            print(f'[{index}/{len(keywords)}]   {keyword}: 抓列表失败 {type(exc).__name__}: {exc}',
                  flush=True)
            continue
        fresh = {row['bill_guid'] for row in rows} - seen
        seen.update(row['bill_guid'] for row in rows)
        done.add(keyword)
        DONE_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding='utf-8')
        print(f'    受理 {len(rows)} 行（新单 {len(fresh)}），累计 {len(seen)} 个单', flush=True)
    print(f'列表抓取完成，共 {len(seen)} 个唯一订单')
    print(json.dumps(plan_summary(), ensure_ascii=False, indent=2))


def plan_summary():
    rows = read_list_rows()
    by_guid = {}
    for row in rows:
        by_guid.setdefault(row['bill_guid'], row)
    templates = {'order': 0, 'bill': 0, 'unknown': 0}
    terminal = 0
    days = []
    for row in by_guid.values():
        template = detail_template(row['code_type'], row['code2'])
        templates['unknown' if template is None else
                  ('order' if 'Order' in template else 'bill')] += 1
        if is_terminal_state(row['state']):
            terminal += 1
        day, _ = normalize_deal_time(row['deal_time'])
        if day:
            days.append(day)
    return {'list_rows': len(rows), 'unique_orders': len(by_guid), 'templates': templates,
            'terminal_state_orders': terminal,
            'fetchable_orders': len(by_guid) - templates['unknown'] - terminal,
            'date_min': min(days) if days else '', 'date_max': max(days) if days else ''}


# ---------------------------------------------------------------------------
# 全量遍历（不带关键词、不带时间窗）
# ---------------------------------------------------------------------------

#: 全量遍历时写进列表行的 ``keyword`` 字段用空串：全量模式里根本没有"哪个词发现它的"
#: 这件事，编一个词出来就是造假（建库时也不会拿它兜底类别）。
SWEEP_KEYWORD = ''


def read_progress():
    if not SWEEP_PROGRESS.exists():
        return {}
    try:
        return json.loads(SWEEP_PROGRESS.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def fetch_page(page, window, platform):
    """抓一页列表（异常直接往上抛，由调用方决定怎么记）。"""
    html = request(platform.list_url, method='POST',
                   data=page_fields(SWEEP_KEYWORD, window, page, platform)
                   ).decode('utf-8', 'replace')
    rows, pages = parse_list_page(html)
    return {'page': page, 'rows': rows, 'pages': pages, 'totals': page_totals(html)}


def command_sweep(args):
    """把结果公示**按页顺序翻到末页**（不带关键词；时间窗可选）。

    为什么不带关键词：站点没有"物品类"这个筛选项（``orderType`` 只有 直购/议价/竞价/反拍），
    要覆盖全部仪器成交就只能按窗口整站遍历，物品类／服务类由列表行自己的 ``code2`` 区分。
    每一批立刻 append 落盘、进度写 ``sweep_progress.json``，**中断后重跑就是接着抓**。

    并发（``--workers``）：请求的**发起间隔仍由产品 ``request()`` 的 ``pace()`` 全局限制在
    ≥1.05 秒**，并发只是让多个请求同时在飞，把"每页 13 秒里有 12 秒在等站点渲染"这段
    等待重叠起来。所以并发数不是"每秒打几个请求"，上限仍是 ~0.95 请求/秒。

    ⚠️ 站点按成交时间倒序、每页 10 行。期间新成交会插到最前面，页边界整体后移——
    后果是**同一单可能被落两次**（不会漏，落库按 ``bill_guid`` 去重）。
    """
    platform = GuangdongXiaolingyigouPlatform()
    today = date.fromisoformat(args.today) if args.today else date.today()
    window = list_window(today, args.window_months) if args.window_months else ('', '')
    progress = {} if args.reset else read_progress()
    first_page = args.from_page or (int(progress.get('last_page') or 0) + 1)
    page = first_page
    workers = max(1, args.workers)
    started = time.monotonic()
    fetched = 0
    total_pages = int(progress.get('total_pages') or 0)
    print(f'时间窗 {window[0] or "（不限）"} ~ {window[1] or "（不限）"}；'
          f'从第 {page} 页开始，并发 {workers}，本轮最多 {args.max_pages or "不限"} 页', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            if args.max_pages and fetched >= args.max_pages:
                print(f'到达 --max-pages={args.max_pages}，本轮收工；下次从第 {page} 页继续')
                break
            if total_pages and page > total_pages:
                print(f'已翻到站点印的末页（第 {total_pages} 页），遍历完成')
                break
            pages = list(range(page, page + workers))
            futures = [(item, pool.submit(fetch_page, item, window, platform)) for item in pages]
            batch, failure, stop = [], None, False
            for item, future in futures:
                try:
                    batch.append(future.result())
                except Exception as exc:
                    failure = (item, f'{type(exc).__name__}: {exc}')
                    break
            if batch:
                for result in batch:
                    rows = result['rows']
                    if not rows:
                        print(f'第 {result["page"]} 页没有行，遍历结束'
                              f'（站点报 {result["totals"]["records"]} 条）')
                        stop = True
                        break
                    _append(LIST_ROWS, _tagged(rows, SWEEP_KEYWORD))
                    fetched += 1
                    total_pages = result['pages'] or result['totals']['pages'] or total_pages
                    progress.update({'last_page': result['page'], 'total_pages': total_pages,
                                     'records': result['totals']['records'],
                                     'rows_seen': int(progress.get('rows_seen') or 0) + len(rows),
                                     'window': list(window), 'workers': workers,
                                     'updated_at': datetime.now().isoformat(timespec='seconds')})
                spent = time.monotonic() - started
                progress['seconds_per_page'] = round(spent / max(fetched, 1), 2)
                progress['pages_fetched'] = fetched
                SWEEP_PROGRESS.parent.mkdir(parents=True, exist_ok=True)
                SWEEP_PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
                last = batch[-1]
                left = max((total_pages or last['page']) - last['page'], 0)
                eta = left * (spent / max(fetched, 1))
                print(f'[{last["page"]}/{total_pages or "?"}] 本批 {len(batch)} 页 '
                      f'{sum(len(r["rows"]) for r in batch)} 行（物品类 '
                      f'{sum(1 for r in batch for row in r["rows"] if row["code2"] == "0")}）'
                      f'  {spent / max(fetched, 1):.1f}s/页  剩余约 {eta / 3600:.1f} 小时',
                      flush=True)
            if failure:
                print(f'第 {failure[0]} 页请求失败 {failure[1]}'
                      f'（已抓的都已落盘，重跑即续）', flush=True)
                break
            if stop:
                break
            page = pages[len(batch) - 1] + 1 if batch else page + workers
            if batch and batch[-1]['rows'] and len(batch[-1]['rows']) < 10:
                print(f'第 {batch[-1]["page"]} 页只有 {len(batch[-1]["rows"])} 行，遍历结束')
                break
    print(json.dumps(read_progress(), ensure_ascii=False, indent=2))



# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------

def fetch_detail(row, log=print):
    """取一单详情：脱敏 → 隐私闸门 → 落盘。返回 ``(guid, status, note)``。"""
    url = detail_url(row)
    if url is None:
        return row['bill_guid'], 'skipped', '未验证的模板，不猜'
    try:
        payload = request(url)
    except Exception as exc:
        return row['bill_guid'], 'error', f'{type(exc).__name__}: {exc}'
    redacted, hits = redact(payload)
    if hits:
        # 闸门有命中就**不写这一页**：个人信息一格都不落盘。
        return row['bill_guid'], 'pii_blocked', '脱敏后仍命中：' + ','.join(hits)
    (DETAILS).mkdir(parents=True, exist_ok=True)
    (DETAILS / f'{row["bill_guid"]}.html').write_text(redacted, encoding='utf-8')
    return row['bill_guid'], 'ok', ''


def command_detail(args):
    rows = read_list_rows()
    by_guid = {}
    for row in rows:
        by_guid.setdefault(row['bill_guid'], row)
    pending = []
    skipped_terminal = 0
    for guid, row in by_guid.items():
        if detail_template(row['code_type'], row['code2']) is None:
            continue
        if is_terminal_state(row['state']):
            skipped_terminal += 1
            continue
        if (DETAILS / f'{guid}.html').exists():
            continue
        pending.append(row)
    pending.sort(key=lambda row: normalize_deal_time(row['deal_time'])[1], reverse=True)
    print(f'唯一订单 {len(by_guid)}，其中终止/取消/重新竞价 {skipped_terminal} 个跳过；'
          f'待抓详情 {len(pending)} 个（已抓 {len(list(DETAILS.glob("*.html"))) if DETAILS.exists() else 0}）')
    started = time.monotonic()
    workers = max(1, args.workers)
    remaining = pending[:args.limit] if args.limit else pending
    print(f'并发 {workers}（请求发起间隔仍由 pace() 限制在 ≥1.05 秒）')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(remaining), workers):
            batch = remaining[start:start + workers]
            future_rows = [(row, pool.submit(fetch_detail, row)) for row in batch]
            for offset, (row, future) in enumerate(future_rows):
                try:
                    guid, status, note = future.result()
                except Exception as exc:  # 单页炸了不许带倒整批
                    guid, status, note = row['bill_guid'], 'error', f'{type(exc).__name__}: {exc}'
                if status != 'ok':
                    _append(DETAIL_GAPS, [{'bill_guid': guid, 'order_no': row['order_no'],
                                           'status': status, 'note': note,
                                           'at': datetime.now().isoformat(timespec='seconds')}])
                index = start + offset + 1
                if index % 25 == 0 or index == len(remaining):
                    spent = time.monotonic() - started
                    print(f'  [{index}/{len(remaining)}] 用时 {spent:.0f}s'
                          f'（{spent / index:.2f}s/单），最近一单 {status}', flush=True)
    print('详情抓取完成')


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description='小零易购结果公示抓取（断点续抓）')
    sub = parser.add_subparsers(dest='command', required=True)

    plan = sub.add_parser('plan', help='按已抓到的列表行汇总抓取计划')
    plan.set_defaults(func=lambda _args: print(json.dumps(plan_summary(), ensure_ascii=False, indent=2)))

    listed = sub.add_parser('list', help='抓列表（按品类词）')
    listed.add_argument('--window-months', type=int, default=24)
    listed.add_argument('--today', default=None)
    listed.add_argument('--max-pages', type=int, default=8)
    listed.add_argument('--keywords', nargs='*', default=None)
    listed.add_argument('--redo', action='store_true', help='已完成过的词也重抓')
    listed.set_defaults(func=command_list)

    detail = sub.add_parser('detail', help='逐单抓详情（可并发）')
    detail.add_argument('--limit', type=int, default=0)
    detail.add_argument('--workers', type=int, default=1, help='同时在飞的请求数（发起间隔仍 ≥1.05 秒）')
    detail.set_defaults(func=command_detail)

    sweep = sub.add_parser('sweep', help='按页顺序遍历结果公示（不带关键词，可选时间窗）')
    sweep.add_argument('--window-months', type=int, default=0, help='0=不限时间窗')
    sweep.add_argument('--today', default=None, help='覆盖"今天"（YYYY-MM-DD），便于复现')
    sweep.add_argument('--workers', type=int, default=1, help='同时在飞的请求数（发起间隔仍 ≥1.05 秒）')
    sweep.add_argument('--max-pages', type=int, default=0, help='本轮最多抓多少页，0=不限')
    sweep.add_argument('--from-page', type=int, default=0, help='从第几页开始（默认接进度）')
    sweep.add_argument('--reset', action='store_true', help='忽略已有进度，从第 1 页重来')
    sweep.set_defaults(func=command_sweep)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    main()
