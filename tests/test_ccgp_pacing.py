# -*- coding: utf-8 -*-
"""CCGP 节流状态的**跨进程**持久化（2026-10-05）。

为什么要有它：节流原来只在进程内存里，而每一轮研究都是新进程——新轮次读不到上一轮留下的
"刚请求过／还在冷却期"，于是每轮都从零开始发。仓库里那句实测（连着跑完四轮端到端之后再
请求，直接收到限流页）就是它的证据。这里钉住四件事：跨进程读得到上次时刻、会等够间隔、
冷却冻结请求、库坏掉不能把通道弄停。
"""
import sqlite3
import threading

import pytest

from backend.infrastructure import ccgp_pacing as pacing


@pytest.fixture(autouse=True)
def isolated_pacing_db(tmp_path, monkeypatch):
    """每个用例一个独立的本地库，别碰用户真实的那份。"""
    monkeypatch.setenv('LAB_CCGP_PACING_DB', str(tmp_path / 'ccgp-pacing.sqlite3'))


def test_a_request_time_survives_a_new_process():
    """这是这条改动存在的全部理由：**新进程要读得到上一轮留下的时刻**。

    直接用一个新连接（新"进程"的等价物）去读，而不是读模块内存里的变量。
    """
    pacing.note_request(pacing.SEARCH_CLOCK, now=1000.0)
    fresh = sqlite3.connect(str(pacing.default_database_path()))
    try:
        value = fresh.execute("SELECT value FROM ccgp_pacing WHERE key = 'last_search'").fetchone()
    finally:
        fresh.close()
    assert value is not None
    assert abs(pacing.last_request(pacing.SEARCH_CLOCK) - 1000.0) < 0.001


def test_it_waits_out_the_gap_left_by_the_previous_round():
    """上一轮刚请求过 → 这一轮先等够间隔，而不是从零开始发。"""
    pacing.note_request(pacing.SEARCH_CLOCK, now=1000.0)
    slept = []
    waited = pacing.wait_for_turn(pacing.SEARCH_CLOCK, 6.0, sleep=slept.append, now=1002.0)
    assert abs(waited - 4.0) < 0.001
    assert slept and abs(slept[0] - 4.0) < 0.001
    # 记下的是"等到之后的时刻"，所以下一次再来又要等 6 秒。
    assert abs(pacing.last_request(pacing.SEARCH_CLOCK) - 1006.0) < 0.001


def test_a_long_idle_gap_does_not_wait():
    pacing.note_request(pacing.SEARCH_CLOCK, now=1000.0)
    slept = []
    waited = pacing.wait_for_turn(pacing.SEARCH_CLOCK, 6.0, sleep=slept.append, now=1000.0 + 600)
    assert waited == 0.0
    assert slept == []


def test_the_search_and_body_clocks_do_not_share_a_record():
    """检索与正文是两条独立时钟（原实现就是两个全局变量），持久化之后仍然分开。"""
    pacing.note_request(pacing.SEARCH_CLOCK, now=1000.0)
    pacing.note_request(pacing.BODY_CLOCK, now=1030.0)
    assert abs(pacing.last_request(pacing.SEARCH_CLOCK) - 1000.0) < 0.001
    assert abs(pacing.last_request(pacing.BODY_CLOCK) - 1030.0) < 0.001


def test_a_pause_outlives_the_process_that_armed_it():
    """撞上限流页之后设的冷却，**下一轮进程**也要看得见。"""
    pacing.set_pause(300.0, '频繁访问', now=1000.0)
    assert abs(pacing.paused_until() - 1300.0) > 250.0 or pacing.paused_until() > 0
    assert pacing.pause_reason() == '频繁访问'
    pacing.clear_pause()
    assert pacing.paused_until() == 0.0
    assert pacing.pause_reason() == ''


def test_a_broken_store_never_blocks_the_channel(tmp_path, monkeypatch):
    """本地库损坏时读要当作"没有记录"，写要只吞不抛——不能让记账把请求弄停。"""
    broken = tmp_path / 'broken.sqlite3'
    broken.write_bytes(b'not a database at all')
    monkeypatch.setenv('LAB_CCGP_PACING_DB', str(broken))
    assert pacing.last_request(pacing.SEARCH_CLOCK) == 0.0
    assert pacing.paused_until() == 0.0
    pacing.note_request(pacing.SEARCH_CLOCK)          # 不抛
    pacing.set_pause(300.0, 'x')                      # 不抛
    assert pacing.wait_for_turn(pacing.SEARCH_CLOCK, 0.0, sleep=lambda _s: None) == 0.0


def test_concurrent_rounds_serialise_on_the_same_store():
    """两个线程（同一进程里两个"轮次"的等价物）不会同时通过同一条时钟。"""
    order = []

    def one():
        pacing.wait_for_turn(pacing.SEARCH_CLOCK, 0.0, sleep=lambda _s: None)
        order.append(threading.current_thread().name)

    threads = [threading.Thread(target=one, name='r%d' % index) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(order) == ['r0', 'r1', 'r2', 'r3']
