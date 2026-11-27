"""第一步：品类词探测。每个词只发一次请求（第一页），记录命中数与分页数。

用法（在仓库根目录）：:

    .venv\\Scripts\\python.exe scripts\\xlyg_probe.py [--window-months 24]

输出 ``output/xlyg-db/probe.json``（被 .gitignore 忽略）。全程串行、同站 ≥1.05 秒，
不登录、不注册、不绕过访问控制。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.education_platform_sources import (  # noqa: E402
    GuangdongXiaolingyigouPlatform, request)
from backend.services.xlyg_local_db import (  # noqa: E402
    PROBE_KEYWORDS, list_window, page_totals, parse_list_page)

OUT = ROOT / 'output/xlyg-db'


def probe(keyword, window, platform=None):
    platform = platform or GuangdongXiaolingyigouPlatform()
    start, end = window
    data = {**platform._form(platform._FIND_TARGET, ''),
            platform.form_keyword_param: keyword,
            'ctl00$ContentPlaceHolder1$txtStateDate': start,
            'ctl00$ContentPlaceHolder1$endStateDate': end}
    html = request(platform.list_url, method='POST', data=data).decode('utf-8', 'replace')
    rows, pages = parse_list_page(html)
    totals = page_totals(html)
    return {
        'keyword': keyword,
        'records': totals['records'],
        'total_pages': pages,
        'page1_rows': len(rows),
        'code_types': sorted({row['code_type'] for row in rows}),
        'states': sorted({row['state'] for row in rows}),
        'page1_dates': [row['deal_time'] for row in rows],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='小零易购品类词探测（每个词一次请求）')
    parser.add_argument('--window-months', type=int, default=24)
    parser.add_argument('--today', default=None, help='覆盖"今天"（YYYY-MM-DD），便于复现')
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()
    window = list_window(today, args.window_months)
    print(f'时间窗 {window[0]} ~ {window[1]}，品类词 {len(PROBE_KEYWORDS)} 个', flush=True)
    results = []
    for i, keyword in enumerate(PROBE_KEYWORDS, 1):
        try:
            item = probe(keyword, window)
        except Exception as exc:  # 被挡就如实记，不绕过
            item = {'keyword': keyword, 'error': f'{type(exc).__name__}: {exc}'}
        results.append(item)
        print(f'[{i}/{len(PROBE_KEYWORDS)}] {keyword}: '
              f'{item.get("records", "-")} 条 / {item.get("total_pages", "-")} 页'
              f'{(" ERR " + item["error"]) if "error" in item else ""}', flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'probe.json').write_text(
        json.dumps({'window': list(window), 'probed_on': today.isoformat(), 'results': results},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    print('写入', OUT / 'probe.json')


if __name__ == '__main__':
    main()
