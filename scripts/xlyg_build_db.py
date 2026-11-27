"""第三步：把抓下来的列表行与详情页**用规则**解析落库。

用法（仓库根目录）：:

    .venv\\Scripts\\python.exe scripts\\xlyg_build_db.py [--db data/xlyg_prices.sqlite3]

只读 ``output/xlyg-db/`` 里的东西，不碰网络。**全程正则/规则，不调用任何模型**：
"哪一列是成交单价"、"这一单是不是终止单"、"这是什么类别"、"型号是不是占位符"
都是页面事实或本模块的固定词表，没有一步需要模型判断。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.xlyg_local_db import (  # noqa: E402
    build, connect, list_window, normalize_deal_time, pii_residue, write_database)

OUT = ROOT / 'output/xlyg-db'
DEFAULT_DB = ROOT / 'data/xlyg_prices.sqlite3'


def load_details():
    details = {}
    folder = OUT / 'details'
    if folder.exists():
        for path in folder.glob('*.html'):
            details[path.stem] = {'html': path.read_text(encoding='utf-8'), 'status': 'ok', 'note': ''}
    gaps = OUT / 'detail_gaps.jsonl'
    if gaps.exists():
        for line in gaps.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            details[record['bill_guid']] = {'html': None, 'status': record['status'],
                                            'note': record['note']}
    return details


def load_list_rows():
    path = OUT / 'list_rows.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def report(result, db_path):
    funnel = result['funnel']
    items = result['items']
    orders = result['orders']
    ok_orders = [row for row in orders if row['fetch_status'] == 'ok']
    dates = sorted(row['deal_date'] for row in items if row.get('deal_date'))
    models = {row['model'].strip() for row in items if row.get('model', '').strip()}
    brands = {row['brand'].strip() for row in items if row.get('brand', '').strip()}
    categories = sorted({row['category'] for row in items if row.get('category')})
    print()
    print('== 漏斗 ==')
    print(f"  列表受理行            {funnel['list_rows']}")
    print(f"  去重后唯一订单        {funnel['unique_orders']}")
    print(f"  同订单号多单→合并     {funnel['merged_duplicate_order_no']}")
    print(f"  未验证模板→跳过       {funnel['skipped_no_template']}")
    print(f"  服务类（有意不接）    {funnel['skipped_service']}")
    print(f"  终止/取消/重新竞价→跳过 {funnel['skipped_terminal']}")
    print(f"  实际取详情页          {funnel['detail_fetched']}")
    print(f"  详情页读成商品表      {funnel['detail_ok']}")
    print(f"  其中有可用成交单价    {funnel['orders_with_price']}")
    print(f"  商品数据行（候选）    {funnel['goods_rows_raw']}")
    print(f"  过全部闸门（verified）{funnel['rows_kept']}")
    print(f"  过 validate_price     {funnel['rows_validate_price']}")
    print()
    print('== 规模 ==')
    print(f'  订单表 {len(orders)} 行（其中 fetch_status=ok {len(ok_orders)}）')
    print(f'  明细行 {len(items)}')
    print(f'  唯一定义型号 {len(models)}（verified 行内 '
          f'{len({row["model"].strip() for row in items if row.get("model") and row.get("verified")})}）')
    print(f'  品牌 {len(brands)}')
    print(f'  类别 {len(categories)}：{"、".join(categories)}')
    print(f'  时间跨度 {dates[0] if dates else "-"} ~ {dates[-1] if dates else "-"}')
    print(f'  库文件 {db_path}')
    print()
    print('== 分布 ==')
    print('  状态（列表页「状态」列）：', _counts(orders, lambda row: row['state']))
    print('  单据类型（codeType）：', _counts(orders, lambda row: row['code_type']))
    print('  价格性质：', _counts(items, lambda row: row['price_kind']))
    print('  单价读自哪一列：', _counts(items, lambda row: row['price_column']))
    print('  类别来源：', _counts(items, lambda row: row['category_source']))
    print('  抓取结果：', _counts(orders, lambda row: row['fetch_status']))
    print('  缺口种类：', _counts(result['gaps'], lambda gap: gap['kind']))
    recent = [row for row in items if row.get('deal_date', '') >= '2024-09-01']
    print(f'  其中「最近 24 个月」（2024-09-01 起）的明细行 {len(recent)}，'
          f'型号 {len({row["model"] for row in recent if row.get("model")})}')
    reasons = {}
    for row in items:
        for reason in (row.get('verify_reasons') or '').split(';'):
            if reason:
                key = reason.split(':')[0]
                reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print('  未过闸门原因统计：', json.dumps(reasons, ensure_ascii=False))


def _counts(rows, key):
    counts = {}
    for row in rows:
        value = key(row) or '(空)'
        counts[value] = counts.get(value, 0) + 1
    return json.dumps(dict(sorted(counts.items(), key=lambda pair: -pair[1])), ensure_ascii=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='小零易购成交明细落库（纯规则）')
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--window-months', type=int, default=None,
                        help='不给就按列表行实际跨度写；给了才用"最近 N 个月"当口径')
    parser.add_argument('--window-start', default=None)
    parser.add_argument('--window-end', default=None)
    parser.add_argument('--today', default=None)
    args = parser.parse_args(argv)

    orders = load_list_rows()
    details = load_details()
    if not orders:
        print('output/xlyg-db/list_rows.jsonl 是空的：先跑 scripts/xlyg_crawl.py sweep')
        return 1
    # 时间口径**以抓到的数据为准**：库自己交代的跨度必须是"里面真有哪些单"，
    # 不是"我想抓哪一段"。参数只在需要改口径时才传。
    if args.window_start or args.window_end:
        window = (args.window_start or '', args.window_end or '')
    elif args.window_months is not None:
        from datetime import date
        window = list_window(date.fromisoformat(args.today) if args.today else date.today(),
                             args.window_months)
    else:
        days = sorted(day for day in
                      (normalize_deal_time(row['deal_time'])[0] for row in orders) if day)
        window = (days[0], days[-1]) if days else ('', '')
    # 有没有"品类词"是数据的属性，不是参数：``sweep`` 抓下来的行 ``keyword`` 是空串，
    # 那时候就没有任何词能拿去兜底类别——编一个出来就是造假。
    keywords = sorted({row.get('keyword') for row in orders if row.get('keyword')})
    print(f'列表行 {len(orders)}（唯一单 {len({r["bill_guid"] for r in orders})}），'
          f'详情页 {sum(1 for d in details.values() if d["status"] == "ok")}，'
          f'缺口记录 {sum(1 for d in details.values() if d["status"] != "ok")}')
    print('发现方式：' + (f'品类词 {len(keywords)} 个：{"、".join(keywords)}' if keywords
                      else '整窗遍历（不带关键词，覆盖窗口内全部单据）'))
    print(f'数据实际跨度 {window[0]} ~ {window[1]}')
    result = build(orders, details, window=window, keywords=keywords)
    db_path = Path(args.db)
    write_database(db_path, result)
    # 建库自检：库里的行数必须与内存结果一致。
    conn = connect(db_path)
    try:
        stored = conn.execute('SELECT COUNT(*) FROM xlyg_item').fetchone()[0]
        stored_orders = conn.execute('SELECT COUNT(*) FROM xlyg_order').fetchone()[0]
        stored_gaps = conn.execute('SELECT COUNT(*) FROM xlyg_gap').fetchone()[0]
    finally:
        conn.close()
    assert stored == len(result['items']), (stored, len(result['items']))
    assert stored_orders == len(result['orders'])
    residue = audit_privacy(db_path)
    report(result, db_path)
    print(f'  缺口表 {stored_gaps} 条')
    print(f'  个人信息残留：{"无" if not residue else residue}')
    return 0


def audit_privacy(db_path):
    """落库之后再扫一遍：库里凡是文本字段，一格个人信息都不许有。"""
    conn = connect(db_path)
    try:
        hits = []
        for row in conn.execute('SELECT order_no, excerpt, price_excerpt FROM xlyg_item'):
            if pii_residue(row['excerpt']) or pii_residue(row['price_excerpt']):
                hits.append(row['order_no'])
        for row in conn.execute('SELECT order_no, project_name, purchaser, supplier, '
                                'supplier_detail FROM xlyg_order'):
            for value in row[1:]:
                if pii_residue(value):
                    hits.append(row['order_no'])
                    break
    finally:
        conn.close()
    return sorted(set(hits))[:10]


if __name__ == '__main__':
    raise SystemExit(main())
