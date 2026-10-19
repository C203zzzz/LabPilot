"""Persistent reservation ledger. No credentials or request contents stored."""
import os
import sqlite3
import time
from pathlib import Path

#: 滚动窗口。``reserve_call`` 与只读的 ``usage`` 用同一个常数，否则"界面说还剩 3 次、
#: 实际已经拦住"这种事迟早发生。
WINDOW_SECONDS = 3600


def _path():
    return Path(os.getenv('LAB_USAGE_DATABASE', 'data/provider-usage.sqlite3'))


def reserve_call(provider, limit):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS attempts (provider TEXT, stamp REAL)')
        db.execute('BEGIN IMMEDIATE')
        now = time.time()
        db.execute('DELETE FROM attempts WHERE stamp < ?', (now - WINDOW_SECONDS,))
        count = db.execute('SELECT COUNT(*) FROM attempts WHERE provider=?', (provider,)).fetchone()[0]
        if count >= limit:
            return False
        db.execute('INSERT INTO attempts VALUES (?,?)', (provider, now))
        return True


def usage(provider, limit):
    """这个 provider 在当前窗口里用掉多少、还剩多少、下一个名额什么时候空出来。

    **只读、不建库**：账本不存在就是"一次都没用过"（建表留给 ``reserve_call``）。
    读失败也不抛——这是一句给用户看的提示，不能因为它自己出错而把请求打掉。

    ``reset_in_seconds`` 是**最早那次调用**满一小时还有多久：额度是滚动窗口，不是整点
    刷新，所以这是"再试"的准确时间，不能笼统写成"一小时后"。
    """
    now = time.time()
    stamps = []
    path = _path()
    if path.exists():
        try:
            with sqlite3.connect(path, timeout=10) as db:
                stamps = [row[0] for row in db.execute(
                    'SELECT stamp FROM attempts WHERE provider=? AND stamp >= ?',
                    (provider, now - WINDOW_SECONDS)).fetchall()]
        except sqlite3.Error:
            stamps = []
    used = len(stamps)
    oldest = min(stamps) if stamps else None
    reset = max(0, int(WINDOW_SECONDS - (now - oldest)) + 1) if oldest is not None else 0
    return {'used': used, 'limit': limit, 'remaining': max(0, limit - used),
            'window_seconds': WINDOW_SECONDS, 'reset_in_seconds': reset}

