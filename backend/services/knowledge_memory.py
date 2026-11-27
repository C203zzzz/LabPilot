# -*- coding: utf-8 -*-
"""沉淀知识库：把**每一轮研究已经拿到的东西**按型号／品类存档，下一轮直接取。

它不是什么
----------
**不是离线建索引，也不是新的抓取管线或检索通道**（这两条用户明确砍掉了）。
这里没有任何网络调用、没有新判据、也不重新裁决任何事：唯一的输入是一轮研究
结束时内存里那份 ``run``，唯一的输出是几行带来源的存档。判据、金额、采信结论
一个字节都不改写——本模块读什么就存什么，取出来还是什么。

存什么（用户 2026-10-06 的定义，见 ``回归基线`` 阶段四）
-------------------------------------------------------------------------------
========  ========  ==========================================================
编号      键        内容
========  ========  ==========================================================
K1        型号      成交价证据：金额、日期、口径、来源 URL、快照哈希、采信/拒收理由
K2        型号      哪个页面写着它**干净**的产品参数（URL ＋ 抽取时间）
K3        型号      型号与品牌在公告里的常见写法
K4        型号      已知不合格来源（读取失败、无金额证据、型号或品牌对不上）
K5        品类      该品类的关键参数与展示顺序
========  ========  ==========================================================

K5 的做法按业务口径：**不专门设计一次模型调用**——模型每轮自己决定展示哪些参数
（就是 ``run['baseline']['parameters']`` 那份有序清单），本模块把它按品类记下来，
跨轮累计成"品类的关键参数与展示顺序"，不人工维护、也不每轮重复问。

与 ``price_memory`` 的分工（刻意不合并）
---------------------------------------
``backend/services/price_memory.py`` 是**线上复用通道**：它记下的价下一轮会被
``_filter_recalled`` 合进 ``run['prices']``，**会影响最终结果**，所以它的规矩很紧
（只记采信过的价、只按型号＋品牌取）。本模块的 K1 是**证据存档**：采信与拒收都记，
还要带当时的口径、理由与快照哈希。两者目的不同、风险不同，因此共用一个 ``data/``
目录下的 SQLite 约定，但**各用各的库**——存档写坏了不会污染复用通道。

硬要求（照 ``设计规范`` §5 的对外口径）
---------------------------------------
* **跳过与丢数据必须能分开**：每一条没写进去的记录都留痕（``ingest_notes`` 里
  一节一条，带原因码与人话原因），整轮的写入／跳过／失败计数进 ``ingest_log``；
* **不把一条的失败放大成整轮失败**：单条记录缺字段只是"这条没得存"，只有数据库
  本身出错才记整轮失败，而且**研究流程继续**（调用方负责兜住异常）；
* 库在 ``data/`` 下，``.gitignore`` 已覆盖 ``data/*`` 与 ``*.sqlite*``，不进 Git。

幂等
----
每条证据都有稳定的 ``evidence_key``（内容自然键），行里记着**见过它的轮次集合**
（``runs_json``）。同一份台账灌两次：第二次遇到"这个轮次已经记过"就整行不动——
条数不翻倍，``times_seen`` 也不会因为重复灌而虚高。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.services.procurement_rules import brand_identity, identity

ROOT = Path(__file__).resolve().parents[2]

#: 这些取值把沉淀库整体关掉（对照跑用，与 ``LAB_PRICE_MEMORY`` 同一个形状）。
_DISABLED = frozenset({'off', '0', 'false', 'no'})

#: K1~K5 五节的名字，对外报数按这个顺序。
SECTIONS = ('K1', 'K2', 'K3', 'K4', 'K5')

EXCERPT_LIMIT = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_evidence (
    evidence_key    TEXT PRIMARY KEY,
    model_key       TEXT NOT NULL,
    brand_key       TEXT NOT NULL DEFAULT '',
    model           TEXT,
    brand           TEXT,
    verdict         TEXT NOT NULL,
    amount          REAL,
    currency        TEXT,
    quantity        REAL,
    deal_date       TEXT,
    scope           TEXT,
    kind            TEXT,
    source_type     TEXT,
    url             TEXT,
    snapshot_sha256 TEXT,
    reason_code     TEXT,
    reason_text     TEXT,
    other_brand     TEXT,
    excerpt         TEXT,
    runs_json       TEXT NOT NULL,
    first_run_id    TEXT,
    task_id         TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_price_evidence_model ON price_evidence(model_key, brand_key);

CREATE TABLE IF NOT EXISTS configuration_evidence (
    evidence_key  TEXT PRIMARY KEY,
    model_key     TEXT NOT NULL,
    model         TEXT,
    brand         TEXT,
    brand_key     TEXT,
    url           TEXT NOT NULL,
    source        TEXT,
    extracted_at  TEXT,
    excerpt       TEXT,
    runs_json     TEXT NOT NULL,
    first_run_id  TEXT,
    task_id       TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_configuration_evidence_model ON configuration_evidence(model_key);

CREATE TABLE IF NOT EXISTS model_spellings (
    evidence_key  TEXT PRIMARY KEY,
    model_key     TEXT NOT NULL,
    model         TEXT,
    spelling_kind TEXT NOT NULL,
    spelling      TEXT NOT NULL,
    spelling_key  TEXT NOT NULL,
    brand         TEXT,
    brand_key     TEXT,
    source_url    TEXT,
    source_type   TEXT,
    runs_json     TEXT NOT NULL,
    first_run_id  TEXT,
    task_id       TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_model_spellings_model ON model_spellings(model_key, spelling_kind);

CREATE TABLE IF NOT EXISTS bad_sources (
    evidence_key  TEXT PRIMARY KEY,
    model_key     TEXT NOT NULL,
    model         TEXT,
    host          TEXT,
    url           TEXT,
    reason_code   TEXT NOT NULL,
    reason_text   TEXT,
    source_type   TEXT,
    runs_json     TEXT NOT NULL,
    first_run_id  TEXT,
    task_id       TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_bad_sources_model ON bad_sources(model_key);
CREATE INDEX IF NOT EXISTS ix_bad_sources_host ON bad_sources(host);

CREATE TABLE IF NOT EXISTS category_parameters (
    evidence_key   TEXT PRIMARY KEY,
    category_key   TEXT NOT NULL,
    category       TEXT,
    parameter_key  TEXT NOT NULL,
    parameter_name TEXT,
    position       INTEGER,
    unit           TEXT,
    direction      TEXT,
    model          TEXT,
    model_key      TEXT,
    brand          TEXT,
    brand_key      TEXT,
    runs_json      TEXT NOT NULL,
    first_run_id   TEXT,
    task_id        TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    times_seen     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_category_parameters_category
    ON category_parameters(category_key, position);

CREATE TABLE IF NOT EXISTS ingest_log (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    task_id      TEXT,
    run_dir      TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    written_json TEXT,
    updated_json TEXT,
    skipped_json TEXT,
    failed_json  TEXT
);

CREATE TABLE IF NOT EXISTS ingest_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_id   TEXT NOT NULL,
    run_id      TEXT,
    section     TEXT,
    outcome     TEXT NOT NULL,
    item        TEXT,
    reason_code TEXT,
    reason_text TEXT,
    noted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ingest_notes_run ON ingest_notes(run_id, outcome);
"""

#: 每张表对应哪一节（对外报数用）。
_TABLE_SECTION = {
    'price_evidence': 'K1',
    'configuration_evidence': 'K2',
    'model_spellings': 'K3',
    'bad_sources': 'K4',
    'category_parameters': 'K5',
}


def enabled() -> bool:
    """默认开启；``LAB_KNOWLEDGE_MEMORY=off`` 关掉（对照跑用）。"""
    return str(os.getenv('LAB_KNOWLEDGE_MEMORY') or '').strip().casefold() not in _DISABLED


def default_database_path() -> Path:
    return Path(os.getenv('LAB_KNOWLEDGE_MEMORY_DB')
                or (ROOT / 'data' / 'knowledge_memory.sqlite3'))


def _connect(path):
    path = Path(path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    # 已经在机器上的旧库要能继续用（``price_memory`` 的 ``kind`` 列同一个理由）：
    # 建表语句里的新列不会加到已存在的表上，这里补一次，缺哪列补哪列。
    for table, columns in (('price_evidence', {'other_brand': 'TEXT'}),
                           ('ingest_log', {'updated_json': 'TEXT'})):
        present = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not present:
            continue
        for name, kind in columns.items():
            if name not in present:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
    return connection


def _read_only(path):
    """读连接：库不存在或还没建表时返回 ``None``（等价于"还没有沉淀"）。"""
    path = Path(path)
    if not path.exists():
        return None
    connection = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _key(function, value) -> str:
    try:
        return function(value)
    except (TypeError, ValueError):
        return ''


def _text(value, limit=None) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] if limit else value


def _evidence_key(*parts) -> str:
    joined = '\x1f'.join('' if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()[:40]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _host(url) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(str(url or '')).hostname or '').casefold()
    except ValueError:
        return ''


def _source_index(run):
    """``run['sources']`` 的两个索引：URL → 快照哈希、URL → 抓取时间。"""
    digests, fetched_at = {}, {}
    for source in run.get('sources') or []:
        if not isinstance(source, dict):
            continue
        url = _text(source.get('url'))
        if not url:
            continue
        snapshot = source.get('snapshot') if isinstance(source.get('snapshot'), dict) else {}
        digest = _text(snapshot.get('sha256')) or _text(source.get('snapshot_sha256'))
        if digest:
            digests.setdefault(url, digest)
        stamp = _text(source.get('retrieved_at')) or _text(source.get('fetched_at'))
        if stamp:
            fetched_at.setdefault(url, stamp)
    return digests, fetched_at


def _baseline(run) -> dict:
    baseline = run.get('baseline')
    return baseline if isinstance(baseline, dict) else {}


# --------------------------------------------------------------------------- #
# 五节的取数：只读 run，不判断、不改写
# --------------------------------------------------------------------------- #

def _price_rows(run, digests, note):
    """K1：``run['prices']`` 是采信的，``run['price_rejections']`` 是被拒的。"""
    rows = []
    for index, row in enumerate(run.get('prices') or []):
        if not isinstance(row, dict):
            note('K1', 'skipped', f'prices[{index}]', 'not_a_row', '价格行不是一条记录，原样跳过。')
            continue
        model = _text(row.get('model'))
        model_key = _key(identity, model)
        if not model_key:
            note('K1', 'skipped', f'prices[{index}]', 'no_model', '这一行没有可归一的型号，无法按型号归档。')
            continue
        amount = row.get('unit_price')
        if amount is None:
            amount = row.get('amount')
        if not _is_number(amount):
            note('K1', 'skipped', f'prices[{index}]', 'no_amount',
                 '这一行没有数值金额（只有线索或文字描述），不当作成交价证据。')
            continue
        url = _text(row.get('url'))
        deal_date = _text(row.get('date')) or _text(row.get('deal_date'))
        scope = (_text(row.get('comparison_level')) or _text(row.get('level'))
                 or _text(row.get('model_reference')))
        excerpt = _text(row.get('excerpt'), EXCERPT_LIMIT)
        reason_text = _text(row.get('scope_note')) or (f'本轮按 {scope} 口径采信。' if scope else '本轮采信。')
        rows.append({
            'evidence_key': _evidence_key('K1', 'accepted', model_key, amount, deal_date, url,
                                          excerpt),
            'model_key': model_key,
            'brand_key': _key(brand_identity, row.get('brand')),
            'model': model,
            'brand': _text(row.get('brand')),
            'verdict': 'accepted',
            'amount': float(amount),
            'currency': _text(row.get('currency')),
            'quantity': row.get('quantity') if _is_number(row.get('quantity')) else None,
            'deal_date': deal_date,
            'scope': scope,
            'kind': _text(row.get('kind')),
            'source_type': _text(row.get('source_type')) or _text(row.get('channel')),
            'url': url,
            'snapshot_sha256': _text(row.get('snapshot_sha256')) or digests.get(url, ''),
            'reason_code': '',
            'reason_text': reason_text,
            'other_brand': '',
            'excerpt': excerpt,
        })
    # 拒收记录的品牌键取**本轮目标品牌**：这条证据是"针对这台机器的这一轮"，不是
    # 引文里那个对不上的牌子——把 other_brand 当品牌键，会让它在下一次同型号研究
    # 按品牌取回时被自己的品牌闸筛掉（等于白记）。
    target_brand = _text(_baseline(run).get('brand'))
    for index, row in enumerate(run.get('price_rejections') or []):
        if not isinstance(row, dict):
            note('K1', 'skipped', f'price_rejections[{index}]', 'not_a_row',
                 '拒收记录不是一条记录，原样跳过。')
            continue
        model_key = _key(identity, row.get('model'))
        if not model_key:
            note('K1', 'skipped', f'price_rejections[{index}]', 'no_model',
                 '这条拒收记录没有可归一的型号，无法按型号归档。')
            continue
        url = _text(row.get('url'))
        if not url:
            note('K1', 'skipped', f'price_rejections[{index}]', 'no_url',
                 '这条拒收记录没有来源地址，归档了也追不回去。')
            continue
        reason_code = _text(row.get('reason_code'))
        excerpt = _text(row.get('excerpt'), EXCERPT_LIMIT)
        rows.append({
            'evidence_key': _evidence_key('K1', 'rejected', model_key, url, reason_code, excerpt),
            'model_key': model_key,
            'brand_key': _key(brand_identity, target_brand),
            'model': _text(row.get('model')),
            'brand': target_brand,
            'verdict': 'rejected',
            'amount': None,
            'currency': '',
            'quantity': None,
            'deal_date': '',
            'scope': _text(row.get('publication_tier')),
            'kind': '',
            'source_type': _text(row.get('source_type')),
            'url': url,
            'snapshot_sha256': _text(row.get('snapshot_sha256')) or digests.get(url, ''),
            'reason_code': reason_code,
            'reason_text': _text(row.get('detail')),
            'other_brand': _text(row.get('other_brand')),
            'excerpt': excerpt,
        })
    return rows


def _configuration_rows(run, fetched_at, note):
    """K2：配置出处 = 候选／价格行上既有的 ``configuration_url`` ＋ ``configuration_source``。"""
    rows = []
    seen = set()

    def add(model, brand, url, source, configuration, label):
        model_key = _key(identity, model)
        url = _text(url)
        source = _text(source)
        configuration = _text(configuration, EXCERPT_LIMIT)
        if not url and not source and not configuration:
            # 这条候选本来就没有配置出处（没网址、没来源标记、没配置文本）：这不是
            # "跳过了一条素材"，记一条跳过只会把真实的跳过淹掉。
            return
        if not model_key:
            note('K2', 'skipped', label, 'no_model', '这一条没有可归一的型号，无法按型号归档。')
            return
        if not url:
            note('K2', 'skipped', label, 'no_url', '这一条写着配置出处却没有地址，归档了也追不回参数页。')
            return
        fingerprint = (model_key, url, source)
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        rows.append({
            'evidence_key': _evidence_key('K2', model_key, url, source),
            'model_key': model_key,
            'model': _text(model),
            'brand': _text(brand),
            'brand_key': _key(brand_identity, brand),
            'url': url,
            'source': source,
            'extracted_at': fetched_at.get(url, ''),
            'excerpt': configuration,
        })

    for index, row in enumerate(run.get('prices') or []):
        if not isinstance(row, dict):
            continue
        add(row.get('model'), row.get('brand'), row.get('configuration_url'),
            row.get('configuration_source'), row.get('configuration'), f'prices[{index}]')
    for name in ('alternatives', 'discovered_candidates', 'reference_candidates'):
        for index, candidate in enumerate(run.get(name) or []):
            if not isinstance(candidate, dict):
                continue
            add(candidate.get('model'), candidate.get('brand'), candidate.get('configuration_url'),
                candidate.get('configuration_source'), candidate.get('configuration'),
                f'{name}[{index}]')
    return rows


def _spelling_rows(run, note):
    """K3：型号与品牌在本轮来源／公告里真实出现过的写法（原文串，不归一）。"""
    rows = []
    seen = set()
    candidates = list(run.get('alternatives') or []) + list(run.get('discovered_candidates') or []) \
        + list(run.get('reference_candidates') or [])
    interesting = {}
    baseline = _baseline(run)

    def remember(model):
        model_key = _key(identity, model)
        if model_key:
            interesting.setdefault(model_key, _text(model))
        return model_key

    remember(baseline.get('model'))
    for candidate in candidates:
        if isinstance(candidate, dict):
            remember(candidate.get('model'))

    def add(model, brand, kind, spelling, source_url, source_type):
        model_key = _key(identity, model)
        spelling = _text(spelling)
        if not model_key or not spelling:
            return
        spelling_key = _key(identity, spelling) if kind == 'model' else _key(brand_identity, spelling)
        if not spelling_key:
            return
        # 键里带**写法原文**：同一台机器在同一页上写了 ``HVE50`` 与 ``HVE-50`` 是两条
        # 写法（K3 要收的就是这个），只有归一后的键会把它们并成一条。
        fingerprint = (model_key, kind, spelling, _text(source_url))
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        rows.append({
            'evidence_key': _evidence_key('K3', model_key, kind, spelling, source_url),
            'model_key': model_key,
            'model': _text(model),
            'spelling_kind': kind,
            'spelling': spelling,
            'spelling_key': spelling_key,
            'brand': _text(brand),
            'brand_key': _key(brand_identity, brand),
            'source_url': _text(source_url),
            'source_type': _text(source_type),
        })

    # 型号写法：一是台账行自己的原文串，二是来源正文里逐字写着的同型号写法。
    for row in list(run.get('prices') or []) + list(run.get('price_rejections') or []):
        if isinstance(row, dict):
            add(row.get('model'), row.get('brand'), 'model', row.get('model'),
                row.get('url'), row.get('source_type') or row.get('publication_tier'))
    for candidate in candidates:
        if isinstance(candidate, dict):
            add(candidate.get('model'), candidate.get('brand'), 'model', candidate.get('model'),
                candidate.get('url') or candidate.get('configuration_url'), 'candidate')

    # 正文扫描与「配置所属型号」同一把尺子（:func:`model_tokens_in_text` 用的同一个
    # 正则、同一套 ``_is_plausible_model`` 判据），**只是不按归一结果去重**：那个函数
    # 会把 ``HVE50`` 与 ``HVE-50`` 折叠成一个，而 K3 要收的恰恰是被折叠掉的那一份写法。
    # 这里既不新造型号词表，也不放宽判据。
    from backend.services.price_rejections import _is_plausible_model
    from backend.services.procurement_research import _MODEL_TOKEN_WITH_DOT, _URL_IN_TEXT
    scanned = 0
    for index, source in enumerate(run.get('sources') or []):
        if not isinstance(source, dict) or source.get('body_status') != 'fetched':
            continue
        body = _text(source.get('body_text'))
        if not body:
            continue
        tokens = [token for token in _MODEL_TOKEN_WITH_DOT.findall(_URL_IN_TEXT.sub(' ', body))
                  if _is_plausible_model(token, '')]
        if not tokens:
            continue
        scanned += 1
        for token in tokens:
            model_key = _key(identity, token)
            if model_key not in interesting:
                continue
            add(interesting[model_key], '', 'model', token, source.get('url'), source.get('source_type'))
    if not scanned and not rows:
        note('K3', 'skipped', 'sources', 'no_readable_body',
             '本轮没有读到可扫描的正文，型号与品牌写法无从收（不是丢掉，是没有素材）。')

    # 品牌写法：本轮台账里真实出现过的品牌原文串，归一到同一家的才算同一写法。
    for row in list(run.get('prices') or []):
        if isinstance(row, dict):
            add(row.get('model'), row.get('brand'), 'brand', row.get('brand'),
                row.get('url'), row.get('source_type') or row.get('channel'))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        add(candidate.get('model'), candidate.get('brand'), 'brand', candidate.get('brand'),
            candidate.get('url') or candidate.get('configuration_url'), 'candidate')
    if baseline.get('brand') and baseline.get('model'):
        add(baseline.get('model'), baseline.get('brand'), 'brand', baseline.get('brand'), '', 'baseline')
    return rows


def _bad_source_rows(run, note):
    """K4：已知不合格来源 = 本轮三条既有账里点名过的地址。"""
    rows = []
    baseline = _baseline(run)

    def add(model, url, reason_code, reason_text, source_type, label):
        model_key = _key(identity, model)
        url = _text(url)
        if not url:
            note('K4', 'skipped', label, 'no_url', '这一条没有地址，留不下可复查的来源。')
            return
        if not model_key:
            note('K4', 'skipped', label, 'no_model', '这一条没有可归一的型号，无法按型号归档。')
            return
        rows.append({
            'evidence_key': _evidence_key('K4', model_key, url, reason_code),
            'model_key': model_key,
            'model': _text(model),
            'host': _host(url),
            'url': url,
            'reason_code': _text(reason_code),
            'reason_text': _text(reason_text, EXCERPT_LIMIT),
            'source_type': _text(source_type),
        })

    for index, row in enumerate(run.get('price_rejections') or []):
        if isinstance(row, dict):
            add(row.get('model'), row.get('url'), row.get('reason_code') or 'price_rejected',
                row.get('detail'), row.get('publication_tier'), f'price_rejections[{index}]')
    for index, row in enumerate(run.get('price_source_skips') or []):
        if isinstance(row, dict):
            add(row.get('target_model') or baseline.get('model'), row.get('url'),
                row.get('skip_reason') or 'source_skipped',
                f"这一页没有金额证据（正文 {row.get('body_len') or 0} 字），价格扫描跳过。",
                row.get('source_type'), f'price_source_skips[{index}]')
    for index, row in enumerate(run.get('unavailable_sources') or []):
        if isinstance(row, dict):
            add(baseline.get('model'), row.get('url'), 'unreadable', row.get('reason'),
                'unavailable', f'unavailable_sources[{index}]')
    return rows


def _category_parameter_rows(run, category, note):
    """K5：本轮模型自己决定的参数清单，按品类记下名字与展示次序。"""
    baseline = _baseline(run)
    category_name = _text(category) or _text(baseline.get('category'))
    category_key = _key(identity, category_name) or category_name.casefold()
    parameters = [item for item in (baseline.get('parameters') or []) if isinstance(item, dict)]
    if not category_name:
        note('K5', 'skipped', 'baseline', 'no_category',
             '这一轮没有品类名（比较基准的品类是空的），参数表不知道该挂到哪个品类下。')
        return []
    if not parameters:
        note('K5', 'skipped', 'baseline.parameters', 'no_parameters',
             '这一轮比较基准里没有参数清单，没有可沉淀的展示顺序。')
        return []
    rows = []
    for index, parameter in enumerate(parameters):
        name = _text(parameter.get('name'))
        parameter_key = _key(identity, name) or name.casefold()
        if not parameter_key:
            note('K5', 'skipped', f'baseline.parameters[{index}]', 'no_parameter_name',
                 '这一项没有参数名，记下来也认不出是什么。')
            continue
        rows.append({
            'evidence_key': _evidence_key('K5', category_key, parameter_key),
            'category_key': category_key,
            'category': category_name,
            'parameter_key': parameter_key,
            'parameter_name': name,
            'position': index + 1,
            'unit': _text(parameter.get('unit')),
            'direction': _text(parameter.get('direction')),
            'model': _text(baseline.get('model')),
            'model_key': _key(identity, baseline.get('model')),
            'brand': _text(baseline.get('brand')),
            'brand_key': _key(brand_identity, baseline.get('brand')),
        })
    return rows


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

def _load_runs(value) -> list:
    try:
        loaded = json.loads(value or '[]')
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _store(connection, table, row, seen_run, now, mutable=()):
    """按 ``evidence_key`` 去重写入；重复见到只记轮次，不新增行。

    ``mutable`` 是"会随轮次更新"的列（只有 K5 用得上：同一个品类下模型每轮决定的
    展示次序可能不同，取回时该看**最近一轮**的顺序）。其余各节的证据行是当时的事实
    ——金额、日期、理由、快照哈希一旦记下就不再改写。
    """
    key = row['evidence_key']
    existing = connection.execute(
        f'SELECT runs_json FROM {table} WHERE evidence_key = ?', (key,)).fetchone()
    if existing is not None:
        runs = _load_runs(existing['runs_json'])
        if seen_run in runs:
            return 'duplicate'
        runs.append(seen_run)
        assignments = ['runs_json = ?', 'times_seen = ?', 'last_seen_at = ?']
        parameters = [json.dumps(runs, ensure_ascii=False), len(runs), now]
        for column in mutable:
            assignments.append(f'{column} = ?')
            parameters.append(row.get(column))
        parameters.append(key)
        connection.execute(f'UPDATE {table} SET {", ".join(assignments)}'
                           ' WHERE evidence_key = ?', parameters)
        return 'updated'
    payload = dict(row)
    payload['runs_json'] = json.dumps([seen_run], ensure_ascii=False)
    payload['first_seen_at'] = now
    payload['last_seen_at'] = now
    payload['times_seen'] = 1
    columns = ', '.join(payload)
    placeholders = ', '.join('?' for _ in payload)
    connection.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})',
                       list(payload.values()))
    return 'inserted'


def record_run(run, *, category=None, run_dir=None, database_path=None) -> dict:
    """把一轮研究的成果写进沉淀库，返回 ``{written, skipped, failed, counts}``。

    本函数**不抛**:调用点（研究收尾）不该因为存档出问题而整轮失败——库写不动
    就记一条 ``failed``，研究结果照旧。``run`` 必须是这一轮完整的那份 payload
    （``prices`` / ``price_rejections`` / ``sources`` / ``alternatives`` /
    ``baseline`` 都在里面）。
    """
    started = _now()
    summary = {'written': {section: 0 for section in SECTIONS},
               #: 以前轮次已经存过、这一轮又见到的条数（行不动，只把轮次记上）；
               #: ``written`` 是**这一轮新落库的行数**，两者相加才是"这一轮往库里
               #: 落了这么多条"，库内总行数看 :func:`counts`。
               'updated': {section: 0 for section in SECTIONS},
               'skipped': {}, 'failed': {},
               #: 同一轮里同一条证据被列了两次（本轮内重复）／这条证据在**同一轮**
               #: 已经存过（重复灌同一份台账）。后者是验收"灌两次不翻倍"看的那个数。
               'duplicates_in_run': 0, 'already_stored': 0,
               'run_id': '', 'task_id': '', 'database': ''}
    if not enabled() or not isinstance(run, dict):
        summary['disabled'] = not enabled()
        return summary
    run_id = _text(run.get('id')) or _text(run.get('run_id')) or 'unknown'
    task_id = _text(run.get('task_id'))
    path = Path(database_path or default_database_path())
    summary.update(run_id=run_id, task_id=task_id, database=str(path))
    notes = []

    def note(section, outcome, item, reason_code, reason_text):
        notes.append({'section': section, 'outcome': outcome, 'item': item,
                      'reason_code': reason_code, 'reason_text': reason_text})
        bucket = summary['skipped'] if outcome == 'skipped' else summary['failed']
        bucket[reason_code] = bucket.get(reason_code, 0) + 1

    connection = None
    ingest_id = f'{run_id}:{started}'
    try:
        digests, fetched_at = _source_index(run)
        groups = (
            ('price_evidence', _price_rows(run, digests, note), ()),
            ('configuration_evidence', _configuration_rows(run, fetched_at, note), ()),
            ('model_spellings', _spelling_rows(run, note), ()),
            ('bad_sources', _bad_source_rows(run, note), ()),
            # K5 的展示次序看最近一轮：同一个品类下模型每轮决定的顺序可能不同。
            ('category_parameters', _category_parameter_rows(run, category, note),
             ('position', 'unit', 'direction', 'model', 'model_key', 'brand', 'brand_key')),
        )
        connection = _connect(path)
        seen_keys = set()
        with connection:
            for table, rows, mutable in groups:
                section = _TABLE_SECTION[table]
                for row in rows:
                    key = row['evidence_key']
                    if key in seen_keys:
                        summary['duplicates_in_run'] += 1
                        continue
                    seen_keys.add(key)
                    row = dict(row, runs_json='[]', first_run_id=run_id,
                               task_id=task_id, first_seen_at=started, last_seen_at=started,
                               times_seen=1)
                    try:
                        outcome = _store(connection, table, row, run_id, started, mutable)
                    except sqlite3.Error as exc:
                        note(section, 'failed', key, 'store_error', _text(str(exc)))
                        continue
                    if outcome == 'duplicate':
                        summary['already_stored'] += 1
                    elif outcome == 'updated':
                        summary['updated'][section] += 1
                    else:
                        summary['written'][section] += 1
            connection.execute(
                'INSERT INTO ingest_log (id, run_id, task_id, run_dir, started_at, finished_at,'
                ' written_json, updated_json, skipped_json, failed_json)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ingest_id, run_id, task_id, _text(run_dir), started, _now(),
                 json.dumps(summary['written'], ensure_ascii=False),
                 json.dumps(summary['updated'], ensure_ascii=False),
                 json.dumps(summary['skipped'], ensure_ascii=False),
                 json.dumps(summary['failed'], ensure_ascii=False)))
            for record in notes:
                connection.execute(
                    'INSERT INTO ingest_notes (ingest_id, run_id, section, outcome, item,'
                    ' reason_code, reason_text, noted_at) VALUES (?,?,?,?,?,?,?,?)',
                    (ingest_id, run_id, record['section'], record['outcome'], record['item'],
                     record['reason_code'], record['reason_text'], _now()))
    except sqlite3.Error as exc:
        summary['failed']['database_error'] = summary['failed'].get('database_error', 0) + 1
        summary['error'] = _text(str(exc))
    finally:
        if connection is not None:
            connection.close()
    summary['notes'] = notes
    return summary


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def _row_to_dict(row) -> dict:
    item = {key: row[key] for key in row.keys()}
    item['runs'] = _load_runs(item.pop('runs_json', '[]'))
    item['times_seen'] = item.get('times_seen')
    return item


def _query(database_path, sql, parameters) -> list:
    connection = _read_only(database_path or default_database_path())
    if connection is None:
        return []
    try:
        return [_row_to_dict(row) for row in connection.execute(sql, parameters).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def recall_model(model, brand=None, *, database_path=None, limit=50) -> dict:
    """取回某个型号的 K1~K4 沉淀（有品牌就按品牌收窄；对不上返回空，不在品牌上放宽）。

    返回的每一行都带着它从哪一轮来的（``runs`` / ``first_run_id``）与来源 URL，
    调用方可以据此回到原始台账。本函数只读，不改任何结论。
    """
    model_key = _key(identity, model)
    result = {'model_key': model_key, 'model': _text(model),
              'brand_key': _key(brand_identity, brand) if brand else '',
              'price_evidence': [], 'configuration_evidence': [],
              'model_spellings': [], 'bad_sources': []}
    if not model_key:
        return result
    brand_clause = ' AND brand_key = ?' if brand else ''
    brand_parameters = [_key(brand_identity, brand)] if brand else []
    result['price_evidence'] = _query(
        database_path,
        'SELECT * FROM price_evidence WHERE model_key = ?' + brand_clause +
        ' ORDER BY deal_date DESC, amount ASC LIMIT ?',
        [model_key, *brand_parameters, int(limit)])
    result['configuration_evidence'] = _query(
        database_path,
        'SELECT * FROM configuration_evidence WHERE model_key = ?'
        ' ORDER BY times_seen DESC, last_seen_at DESC LIMIT ?',
        [model_key, int(limit)])
    result['model_spellings'] = _query(
        database_path,
        'SELECT * FROM model_spellings WHERE model_key = ?'
        ' ORDER BY spelling_kind, times_seen DESC, spelling LIMIT ?',
        [model_key, int(limit)])
    result['bad_sources'] = _query(
        database_path,
        'SELECT * FROM bad_sources WHERE model_key = ?'
        ' ORDER BY times_seen DESC, last_seen_at DESC LIMIT ?',
        [model_key, int(limit)])
    return result


def recall_category(category, *, database_path=None, limit=100) -> dict:
    """取回某个品类的 K5 参数表：见过次数多的在前，同次数按展示次序。"""
    name = _text(category)
    category_key = _key(identity, name) or name.casefold()
    result = {'category_key': category_key, 'category': name, 'parameters': []}
    if not category_key:
        return result
    rows = _query(
        database_path,
        'SELECT * FROM category_parameters WHERE category_key = ?'
        ' ORDER BY times_seen DESC, position ASC LIMIT ?',
        [category_key, int(limit)])
    result['parameters'] = rows
    return result


def recall(target, *, category=None, database_path=None) -> dict:
    """按一份研究目标（``{'model','brand','category'}``）一次取回能取到的沉淀。"""
    target = target if isinstance(target, dict) else {}
    payload = {'model': recall_model(target.get('model'), target.get('brand'),
                                    database_path=database_path)}
    category_name = _text(category) or _text(target.get('category'))
    if category_name:
        payload['category'] = recall_category(category_name, database_path=database_path)
    return payload


def counts(*, database_path=None) -> dict:
    """库里按 K1~K5 分的行数（验收与体检用）。"""
    result = {section: 0 for section in SECTIONS}
    connection = _read_only(database_path or default_database_path())
    if connection is None:
        return result
    try:
        for table, section in _TABLE_SECTION.items():
            try:
                result[section] = connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            except sqlite3.OperationalError:
                result[section] = 0
    finally:
        connection.close()
    return result


def ingest_history(*, database_path=None) -> list:
    """灌库留痕：每一轮写了什么、跳过多少、失败多少。"""
    return _query(database_path, 'SELECT * FROM ingest_log ORDER BY started_at', [])
