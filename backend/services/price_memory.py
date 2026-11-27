# -*- coding: utf-8 -*-
"""价格记忆：把**已经采信**的成交价按「型号 + 品牌」记下来，下一轮先查它。

动机
----
批2 四份 run 里三份 ``prices=0``，而每一轮都从零重新检索：同一台机器第二次被问到时，
上一轮已经核验过的价本该直接可用。``data/xlyg_prices.sqlite3`` 就是这个思路的成功样板
——一次建库 1634 条明细，长期可查。

为什么比离线库危险
------------------
离线库是抓来的原始成交事实，这里的每一条都是**本产品自己认定过的价**。错价一旦入库
会被反复复用，而"复用了三轮"看起来比"新查到一个"更可信。所以规矩定死：

* **只记采信过的价**（``run['prices']`` 里留下的行），不记线索、不记被拒的；
* 每条带**它当时的可比性级别**（``model_reference``）与**首次记录时间**：判据会变，
  记忆必须能追回"它当初是按什么标准收下的"；
* 回忆出来的行带 ``from_memory=True`` 与 ``memory_first_seen_at``，与当轮检索的价
  在数据上可区分（界面沿用既有字段，不新增栏位）；
* 库在 ``data/`` 下，``.gitignore`` 已覆盖 ``data/*`` 与 ``*.sqlite*``，不进 Git；
* ``LAB_PRICE_MEMORY=off`` 整体关掉，供对照跑使用。

**本模块不重新裁决任何事**：回忆出来的行原样带着当初的采信结论，这里不解释、不放宽、
不改写金额。它只做存取。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.services.procurement_rules import brand_identity, identity

ROOT = Path(__file__).resolve().parents[2]

#: 这些取值把记忆整体关掉（做对照跑时用）。
_DISABLED = frozenset({'off', '0', 'false', 'no'})

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_memory (
    model_key       TEXT NOT NULL,
    brand_key       TEXT NOT NULL,
    model           TEXT NOT NULL,
    brand           TEXT,
    unit_price      REAL NOT NULL,
    currency        TEXT,
    quantity        REAL,
    deal_date       TEXT,
    url             TEXT,
    source_type     TEXT,
    excerpt         TEXT,
    model_reference TEXT,
    kind            TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (model_key, brand_key, unit_price, url)
);
CREATE INDEX IF NOT EXISTS ix_price_memory_model ON price_memory(model_key);
"""


def enabled() -> bool:
    """默认开启；``LAB_PRICE_MEMORY=off`` 关掉（对照跑用）。"""
    return str(os.getenv('LAB_PRICE_MEMORY') or '').strip().casefold() not in _DISABLED


def default_database_path() -> Path:
    return Path(os.getenv('LAB_PRICE_MEMORY_DB') or (ROOT / 'data' / 'price_memory.sqlite3'))


def _key(function, value):
    """标准化键；空值或不可标准化时返回空串（不抛）。"""
    try:
        return function(value)
    except (TypeError, ValueError):
        return ''


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    # 2026-10-03 之前建的库没有 kind 列。记忆行缺 kind 就过不了下游「只认
    # 中标/合同」的闸（procurement_research 的 report()），价格记忆等于白记。
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(price_memory)')}
    if columns and 'kind' not in columns:
        connection.execute('ALTER TABLE price_memory ADD COLUMN kind TEXT')
    return connection


def _is_price(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def record(run, target, *, database_path=None) -> int:
    """把本轮**采信**的价写进记忆库，返回写入（含更新）的条数。

    只取 ``run['prices']`` 里型号与 ``target['model']`` 相同的行；同一
    (型号, 品牌, 单价, 来源) 再次出现只累加 ``times_seen`` 并刷新 ``last_seen_at``，
    ``first_seen_at`` 保持第一次的值——"这个价最早是什么时候见到的"不能被覆盖。
    """
    if not enabled():
        return 0
    model_key = _key(identity, (target or {}).get('model'))
    if not model_key:
        return 0
    brand = (target or {}).get('brand')
    brand_key = _key(brand_identity, brand)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    written = 0
    connection = _connect(database_path or default_database_path())
    try:
        for row in run.get('prices') or []:
            if not isinstance(row, dict):
                continue
            if _key(identity, row.get('model')) != model_key:
                continue
            price = row.get('unit_price')
            if not _is_price(price):
                continue
            connection.execute(
                'INSERT INTO price_memory (model_key, brand_key, model, brand, unit_price,'
                ' currency, quantity, deal_date, url, source_type, excerpt, model_reference,'
                ' kind, first_seen_at, last_seen_at, times_seen)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)'
                ' ON CONFLICT(model_key, brand_key, unit_price, url) DO UPDATE SET'
                '   last_seen_at = excluded.last_seen_at,'
                '   times_seen = times_seen + 1',
                (model_key, brand_key, str(row.get('model') or ''), brand if brand else None,
                 float(price), row.get('currency'), row.get('quantity'), row.get('date'),
                 row.get('url'), row.get('source_type'), row.get('excerpt'),
                 row.get('model_reference'), row.get('kind'), now, now))
            written += 1
        connection.commit()
    finally:
        connection.close()
    return written


def recall(model, brand, *, database_path=None, limit=20) -> list:
    """按「型号 + 品牌」取回记忆里的成交价，最近见到的优先。

    返回的行**形状与 ``prices()`` 的行一致**，并多两个字段：

    * ``from_memory``：恒为 ``True``，调用方据此区分"这是记着的"与"这是刚查到的"；
    * ``memory_first_seen_at`` / ``memory_times_seen``：这条价最早何时见到、见过几次。

    品牌对不上就返回空——**不在品牌上放宽**：品牌闸是采信判据的一部分，
    记忆库没有资格替它做主（这一点与 ``设计规范`` 的结论同向）。
    """
    if not enabled():
        return []
    model_key = _key(identity, model)
    if not model_key:
        return []
    path = Path(database_path or default_database_path())
    if not path.exists():
        return []
    brand_key = _key(brand_identity, brand)
    connection = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            'SELECT * FROM price_memory WHERE model_key = ? AND brand_key = ?'
            ' ORDER BY deal_date DESC, unit_price ASC LIMIT ?',
            (model_key, brand_key, int(limit))).fetchall()
    except sqlite3.OperationalError:
        # 库存在但还没有建表（例如空文件）：等价于"没有记忆"。
        return []
    finally:
        connection.close()
    return [{
        'model': row['model'],
        'brand': row['brand'],
        'unit_price': row['unit_price'],
        'currency': row['currency'],
        'quantity': row['quantity'],
        'date': row['deal_date'],
        'url': row['url'],
        'source_type': row['source_type'],
        'excerpt': row['excerpt'],
        'model_reference': row['model_reference'],
        # 采信时的类别要原样带回去：下游只认 award/contract，缺了它这条记忆价
        # 会被静默丢掉（批3 实测：记了、取回来了，却没进最终结果）。旧库里
        # 没存过 kind，按 award 兜底——记忆库只记采信过的成交价。
        'kind': ((row['kind'] if 'kind' in row.keys() else None) or 'award'),
        'from_memory': True,
        'memory_first_seen_at': row['first_seen_at'],
        'memory_times_seen': row['times_seen'],
    } for row in rows]
