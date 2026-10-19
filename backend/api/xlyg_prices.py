"""小零易购成交明细本地库的查询端点。

数据是 ``scripts/xlyg_crawl.py`` + ``scripts/xlyg_build_db.py`` 抓下来、**纯规则**
解析出来的本地 SQLite（默认 ``data/xlyg_prices.sqlite3``，可用 ``LAB_XLYG_DB`` 改）。
这里只读，不写；库不在就明确回 503，不装作"没有数据"。

边界（端点自己也印在响应里）：这个库只有**广东省教育系统**的小额采购成交记录，
代表不了全国行情，**不能当通用价格基准**用。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]

#: 这个库的适用范围，跟着每条响应一起回去——断章取义地拿它当全国价是这库最容易
#: 被误用的方式，所以这句话不能只写在文档里。
SCOPE_NOTE = ('本库仅覆盖广东省教育部门“小零易购”电子采购系统的成交记录'
              '（小额采购，单一省份、单一渠道），代表不了全国行情，不能当通用价格基准。')

ITEM_COLUMNS = ('order_no', 'line_no', 'goods_name', 'product_name', 'brand', 'model',
                'quantity', 'unit_price', 'amount', 'price_column', 'price_kind',
                'category', 'category_source', 'deal_date', 'purchaser', 'supplier',
                'detail_url', 'excerpt', 'table_header', 'price_excerpt', 'verified',
                'verify_reasons', 'source_keywords', 'order_total_consistent',
                'validate_price', 'validate_note')


def default_database_path() -> Path:
    return Path(os.getenv('LAB_XLYG_DB') or (ROOT / 'data/xlyg_prices.sqlite3'))


def _connect(path):
    if not Path(path).exists():
        raise HTTPException(503, detail={
            'code': 'xlyg_database_missing',
            'message': f'本地库还没建：{path}。先跑 scripts/xlyg_crawl.py 与 '
                       f'scripts/xlyg_build_db.py。',
        })
    conn = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params)]


def _like(value):
    """型号／品牌／类别都走"包含"匹配：调用方给 ``H1850`` 就该把 ``H1850`` 找出来，
    不需要它知道库里存的是 ``H1850`` 还是 ``H1850 型``。"""
    return f'%{value.strip()}%'


def xlyg_router(database_path=None):
    path = Path(database_path) if database_path is not None else default_database_path()
    router = APIRouter(prefix='/api/v1/xlyg', tags=['xlyg-local-db'])

    def where(model, brand, category, date_from, date_to, verified_only, price_kind):
        clauses, params = [], []
        if model:
            clauses.append('model LIKE ?')
            params.append(_like(model))
        if brand:
            clauses.append('brand LIKE ?')
            params.append(_like(brand))
        if category:
            clauses.append('category LIKE ?')
            params.append(_like(category))
        if date_from:
            clauses.append('deal_date >= ?')
            params.append(date_from)
        if date_to:
            clauses.append('deal_date <= ?')
            params.append(date_to)
        if verified_only:
            clauses.append('verified = 1')
        if price_kind:
            clauses.append('price_kind = ?')
            params.append(price_kind)
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params

    @router.get('/prices')
    def prices(model: str = '', brand: str = '', category: str = '',
               date_from: str = '', date_to: str = '',
               price_kind: str = '', verified_only: bool = True,
               limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)):
        """**主用途：按型号查成交明细。** 也可按品牌／类别／成交日期区间筛。

        ``verified_only=true``（默认）只回过了全部闸门的行：型号非空且不是占位符、
        逐字出现在商品表数据行里、单价为正、Σ单价×数量 与页面整单金额自洽、
        产品自己的 ``validate_price`` 收下。要连未过的行一起看就传 ``false``。
        """
        clause, params = where(model, brand, category, date_from, date_to, verified_only, price_kind)
        conn = _connect(path)
        try:
            total = conn.execute('SELECT COUNT(*) FROM xlyg_item' + clause, params).fetchone()[0]
            items = _rows(conn, 'SELECT ' + ','.join(ITEM_COLUMNS) + ' FROM xlyg_item' + clause
                          + ' ORDER BY deal_date DESC, order_no, line_no LIMIT ? OFFSET ?',
                          [*params, limit, offset])
        finally:
            conn.close()
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                'filters': {'model': model, 'brand': brand, 'category': category,
                            'date_from': date_from, 'date_to': date_to,
                            'price_kind': price_kind, 'verified_only': verified_only},
                'database': str(path), 'scope_note': SCOPE_NOTE}

    @router.get('/models')
    def models(q: str = '', limit: int = Query(200, ge=1, le=2000)):
        """型号清单 + 每个型号的成交条数：先看"库里有这个型号吗"再取明细。"""
        conn = _connect(path)
        try:
            items = _rows(conn, """
                SELECT model, COUNT(*) AS rows, COUNT(DISTINCT order_no) AS orders,
                       MIN(deal_date) AS first_date, MAX(deal_date) AS last_date,
                       MIN(unit_price) AS min_price, MAX(unit_price) AS max_price,
                       GROUP_CONCAT(DISTINCT brand) AS brands
                FROM xlyg_item
                WHERE verified = 1 AND model <> '' """ + ('AND model LIKE ? ' if q else '') + """
                GROUP BY model ORDER BY rows DESC, model LIMIT ?""",
                          ([_like(q)] if q else []) + [limit])
        finally:
            conn.close()
        return {'items': items, 'database': str(path), 'scope_note': SCOPE_NOTE}

    @router.get('/categories')
    def categories():
        """列出类别及各自的成交条数／型号数／品牌数／时间跨度。"""
        conn = _connect(path)
        try:
            items = _rows(conn, """
                SELECT category, COUNT(*) AS rows, COUNT(DISTINCT order_no) AS orders,
                       COUNT(DISTINCT model) AS models, COUNT(DISTINCT brand) AS brands,
                       MIN(deal_date) AS first_date, MAX(deal_date) AS last_date
                FROM xlyg_item
                WHERE verified = 1 AND category <> ''
                GROUP BY category ORDER BY rows DESC, category""")
        finally:
            conn.close()
        return {'items': items, 'database': str(path), 'scope_note': SCOPE_NOTE}

    @router.get('/brands')
    def brands():
        conn = _connect(path)
        try:
            items = _rows(conn, """
                SELECT brand, COUNT(*) AS rows, COUNT(DISTINCT model) AS models,
                       MIN(deal_date) AS first_date, MAX(deal_date) AS last_date
                FROM xlyg_item
                WHERE verified = 1 AND brand <> ''
                GROUP BY brand ORDER BY rows DESC, brand""")
        finally:
            conn.close()
        return {'items': items, 'database': str(path), 'scope_note': SCOPE_NOTE}

    @router.get('/summary')
    def summary():
        """这个库自己交代规模、时间跨度、口径与漏斗；没有库就 503。"""
        conn = _connect(path)
        try:
            meta = {row['key']: row['value'] for row in conn.execute('SELECT key, value FROM xlyg_meta')}
            stats = conn.execute("""
                SELECT COUNT(*) AS rows, COUNT(DISTINCT order_no) AS orders,
                       COUNT(DISTINCT model) AS models, COUNT(DISTINCT brand) AS brands,
                       COUNT(DISTINCT category) AS categories,
                       MIN(deal_date) AS first_date, MAX(deal_date) AS last_date
                FROM xlyg_item WHERE verified = 1""").fetchone()
            kinds = _rows(conn, 'SELECT price_kind, COUNT(*) AS rows FROM xlyg_item '
                                'WHERE verified = 1 GROUP BY price_kind ORDER BY rows DESC')
            gaps = _rows(conn, 'SELECT kind, COUNT(*) AS n FROM xlyg_gap GROUP BY kind ORDER BY n DESC')
        finally:
            conn.close()
        return {'items': dict(stats), 'price_kinds': kinds, 'gaps': gaps,
                'funnel': json.loads(meta.get('funnel') or '{}'),
                'keywords': json.loads(meta.get('keywords') or '[]'),
                'window': {'start': meta.get('window_start', ''), 'end': meta.get('window_end', '')},
                'built_at': meta.get('built_at', ''),
                'database': str(path), 'scope_note': SCOPE_NOTE}

    @router.get('/orders/{order_no}')
    def order(order_no: str):
        """一个订单的全部明细行 + 订单级信息（含"这一单为什么没产出价格"）。"""
        conn = _connect(path)
        try:
            head = conn.execute('SELECT * FROM xlyg_order WHERE order_no = ?',
                                (order_no,)).fetchone()
            if head is None:
                raise HTTPException(404, detail={'code': 'xlyg_order_not_found',
                                                 'message': f'库里没有订单 {order_no}'})
            items = _rows(conn, 'SELECT ' + ','.join(ITEM_COLUMNS)
                          + ' FROM xlyg_item WHERE order_no = ? ORDER BY line_no', (order_no,))
        finally:
            conn.close()
        return {'order': dict(head), 'items': items, 'database': str(path),
                'scope_note': SCOPE_NOTE}

    return router
