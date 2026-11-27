"""CCGP 的节流状态：**跨进程**记住"上次什么时候请求过""现在是不是还在冷却期"。

为什么需要它（2026-10-05 实测）：节流状态原来只活在进程内存里——``_ccgp_last_request``
与 ``_ccgp_body_last_request`` 是模块级全局，冷却截止 ``paused_until`` 是适配器**实例属性**。
而每一轮研究都是**一个新进程**（每轮端到端都起一个新后端），所以新轮次启动时这些值全是 0：
它当自己从没碰过这个站，上来就按 6 秒地板发请求；可站点看到的是**连续几轮累积**下来的流量。
``search_providers.py`` 里那句实测正是它的证据——「连着跑完四轮端到端之后再请求，直接收到
那张 200 字节左右的限流页」。单轮内的地板控得住，**跨轮之间完全没人控**。

这个模块把三件事落进一个本地 sqlite（默认 ``data/ccgp_pacing.sqlite3``，
``LAB_CCGP_PACING_DB`` 可改；与 ``LAB_PRICE_MEMORY_DB``／``LAB_USAGE_DATABASE`` 同一个先例）：

* 两条节奏时钟（检索 ``search`` 与正文 ``body``）各自的"上次请求时刻"；
* **冷却截止时刻**——撞上限流页之后，这一轮与后面几轮都不许再发；
* 冷却的来由（脱敏后的一句话，只给排查看）。

两条纪律：

* **读失败一律当作"没有记录"**：本地库损坏、权限不对，都不能让整条通道停摆或拒发；
* **写失败只吞不抛**：节流记不下来是可惜，不是致命，绝不能让记账把一次真实请求弄挂。

时钟用 ``time.time()``（墙钟）而不是 ``time.monotonic()``：单调钟只在进程内有意义，
跨进程必须用墙钟。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

#: 一次请求之后，同一类时钟至少要等这么久才允许下一次（由调用方传入它自己那档间隔，
#: 这里只保存"上次是什么时候"）。
SEARCH_CLOCK = 'search'
BODY_CLOCK = 'body'

_SCHEMA = ('CREATE TABLE IF NOT EXISTS ccgp_pacing ('
           'key TEXT PRIMARY KEY, value TEXT NOT NULL)')


def default_database_path() -> Path:
    """本地库位置：``LAB_CCGP_PACING_DB`` 优先，默认 ``data/ccgp_pacing.sqlite3``。"""
    return Path(os.getenv('LAB_CCGP_PACING_DB')
                or (Path(__file__).resolve().parents[2] / 'data/ccgp_pacing.sqlite3'))


def _connect(path=None):
    target = Path(path) if path is not None else default_database_path()
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def _read(key, path=None) -> str:
    try:
        conn = _connect(path)
        try:
            row = conn.execute('SELECT value FROM ccgp_pacing WHERE key = ?', (key,)).fetchone()
        finally:
            conn.close()
    except Exception:      # 读失败＝"没有记录"，绝不让它挡住请求
        return ''
    return str(row[0]) if row else ''


def _write(key, value, path=None) -> None:
    try:
        conn = _connect(path)
        try:
            conn.execute('INSERT INTO ccgp_pacing (key, value) VALUES (?, ?) '
                         'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
            conn.commit()
        finally:
            conn.close()
    except Exception:      # 写失败只吞：记账挂了不该把一次真实请求弄挂
        pass


def last_request(kind, path=None) -> float:
    """这一类时钟上次发请求的墙钟时刻；没有记录返回 ``0.0``。"""
    try:
        return float(_read('last_%s' % kind, path) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def note_request(kind, *, now=None, path=None) -> float:
    """记下"刚刚发过一次"，返回记下的时刻。"""
    stamp = float(now if now is not None else time.time())
    _write('last_%s' % kind, repr(stamp), path)
    return stamp


def wait_for_turn(kind, min_interval, *, sleep=time.sleep, now=None, path=None) -> float:
    """等到"离上次请求够 ``min_interval`` 秒"，**先等再记**，返回等了多久。

    跨进程的意义就在这里：新起的一轮进来时读到的 ``last_request`` 是**上一轮**留下的，
    所以它会先把上一轮欠下的间隔等够，而不是从零开始按自己的节奏发。
    """
    stamp = float(now if now is not None else time.time())
    waited = 0.0
    previous = last_request(kind, path)
    if previous and min_interval > 0:
        waited = max(0.0, previous + float(min_interval) - stamp)
        if waited > 0:
            sleep(waited)
    note_request(kind, now=stamp + waited, path=path)
    return waited


def paused_until(path=None) -> float:
    """冷却截止的墙钟时刻；不在冷却期返回 ``0.0``。"""
    try:
        deadline = float(_read('paused_until', path) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return deadline if deadline > time.time() else 0.0


def pause_reason(path=None) -> str:
    return _read('pause_reason', path)


def set_pause(seconds, reason='', *, now=None, path=None) -> float:
    """把冷却写进本地库（**跨轮生效**）：接下来 ``seconds`` 秒内一律不发请求。"""
    deadline = float(now if now is not None else time.time()) + float(seconds)
    _write('paused_until', repr(deadline), path)
    _write('pause_reason', str(reason or ''), path)
    return deadline


def clear_pause(path=None) -> None:
    """清掉冷却。测试与显式恢复用；正常路径不该调它。"""
    _write('paused_until', repr(0.0), path)
    _write('pause_reason', '', path)
