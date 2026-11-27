# -*- coding: utf-8 -*-
"""价格记忆的存取契约。

这一层只做存取，**不重新裁决任何事**：所以测试钉的是"录了什么、召回什么、
关掉之后什么都不做"，而不是价格判据（那些在 ``test_price_rejections.py`` 等处）。
"""
from backend.services import price_memory

TARGET = {'brand': '上海博迅', 'model': 'SSW-420-2S'}


def _run(*prices):
    return {'prices': list(prices)}


def _row(**overrides):
    """一条**已采信**的价格行，形状取自 ``prices()`` 的产出。"""
    row = {'model': 'SSW-420-2S', 'unit_price': 2981.98, 'currency': 'CNY',
           'quantity': 3.0, 'date': '2026-03-11', 'url': 'https://example.invalid/a',
           'source_type': 'tender_document',
           'excerpt': '1 | 恒温电热水槽 | 博讯SSW-420-2S | 台 | 3 | \\ | 2981.98',
           'model_reference': 'model'}
    row.update(overrides)
    return row


def test_record_then_recall_round_trip(tmp_path):
    database = tmp_path / 'memory.sqlite3'
    assert price_memory.record(_run(_row()), TARGET, database_path=database) == 1
    got = price_memory.recall('SSW-420-2S', '上海博迅', database_path=database)
    assert len(got) == 1
    assert got[0]['unit_price'] == 2981.98
    assert got[0]['from_memory'] is True
    assert got[0]['model_reference'] == 'model'
    assert got[0]['memory_times_seen'] == 1


def test_recalled_rows_carry_the_kind_so_downstream_keeps_them(tmp_path):
    """记忆行必须带 kind：下游只认 award/contract，缺了会被静默丢掉。

    批3（2026-10-03）实测：两笔往轮价被 ``recall`` 取回、``price_memory_hits``
    也记了 ``count=2``，最终 ``prices[]`` 里却一条都没有——记忆行没有 kind，
    过不了 ``report()`` 里 ``q.get('kind') in {'award', 'contract'}`` 那道闸。
    """
    database = tmp_path / 'memory.sqlite3'
    assert price_memory.record(_run(_row(kind='award')), TARGET, database_path=database) == 1
    got = price_memory.recall('SSW-420-2S', '上海博迅', database_path=database)
    assert got[0]['kind'] == 'award'
    assert all(row['kind'] in {'award', 'contract'} for row in got)


def test_the_original_kind_is_kept_not_rewritten_to_award(tmp_path):
    database = tmp_path / 'memory.sqlite3'
    price_memory.record(_run(_row(kind='contract')), TARGET, database_path=database)
    got = price_memory.recall('SSW-420-2S', '上海博迅', database_path=database)
    assert got[0]['kind'] == 'contract'


def test_an_old_database_without_the_kind_column_is_upgraded(tmp_path):
    """2026-10-03 之前建的库没有 kind 列：补列后旧行按 award 兜底。"""
    import sqlite3

    from backend.services.procurement_rules import identity

    database = tmp_path / 'memory.sqlite3'
    connection = sqlite3.connect(str(database))
    connection.execute(
        'CREATE TABLE price_memory (model_key TEXT NOT NULL, brand_key TEXT NOT NULL,'
        ' model TEXT NOT NULL, brand TEXT, unit_price REAL NOT NULL, currency TEXT,'
        ' quantity REAL, deal_date TEXT, url TEXT, source_type TEXT, excerpt TEXT,'
        ' model_reference TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,'
        ' times_seen INTEGER NOT NULL DEFAULT 1,'
        ' PRIMARY KEY (model_key, brand_key, unit_price, url))')
    connection.execute(
        'INSERT INTO price_memory (model_key, brand_key, model, brand, unit_price, currency,'
        ' quantity, deal_date, url, source_type, excerpt, model_reference, first_seen_at,'
        ' last_seen_at, times_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (identity('SSW-420-2S'), '博迅', 'SSW-420-2S', '上海博迅', 2981.98, 'CNY', 3.0,
         '2026-03-11', 'https://example.invalid/a', 'tender_document', 'excerpt', 'model',
         '2026-03-11T00:00:00+00:00', '2026-03-11T00:00:00+00:00', 1))
    connection.commit()
    connection.close()
    assert price_memory.record(_run(_row()), TARGET, database_path=database) == 1
    got = price_memory.recall('SSW-420-2S', '上海博迅', database_path=database)
    assert len(got) == 1
    assert got[0]['kind'] == 'award'
    assert got[0]['memory_times_seen'] == 2


def test_spelling_variants_hit_the_same_memory(tmp_path):
    """归一化落到同一条记忆：型号的空格／连字符差异、品牌的别名与异写。"""
    database = tmp_path / 'memory.sqlite3'
    price_memory.record(_run(_row()), TARGET, database_path=database)
    assert price_memory.recall('ssw 420 2s', '博迅', database_path=database)
    assert price_memory.recall('SSW-420-2s', '博讯', database_path=database)


def test_a_different_brand_does_not_recall(tmp_path):
    """品牌对不上就不召回——记忆库没有资格替品牌闸做主。"""
    database = tmp_path / 'memory.sqlite3'
    price_memory.record(_run(_row()), TARGET, database_path=database)
    assert price_memory.recall('SSW-420-2S', '菲斯福', database_path=database) == []


def test_second_sighting_keeps_the_first_timestamp(tmp_path):
    """``first_seen_at`` 是"这个价最早何时见到"，不能被后来的覆盖。"""
    database = tmp_path / 'memory.sqlite3'
    price_memory.record(_run(_row()), TARGET, database_path=database)
    first = price_memory.recall('SSW-420-2S', '上海博迅',
                                database_path=database)[0]['memory_first_seen_at']
    price_memory.record(_run(_row()), TARGET, database_path=database)
    again = price_memory.recall('SSW-420-2S', '上海博迅', database_path=database)[0]
    assert again['memory_first_seen_at'] == first
    assert again['memory_times_seen'] == 2


def test_rows_of_another_model_are_not_recorded(tmp_path):
    """只记目标型号的行：候选型号的价不进这条记忆。"""
    database = tmp_path / 'memory.sqlite3'
    assert price_memory.record(_run(_row(model='WB-12-4', unit_price=1234.0)),
                               TARGET, database_path=database) == 0
    assert price_memory.recall('WB-12-4', '上海博迅', database_path=database) == []


def test_a_row_without_a_usable_price_is_not_recorded(tmp_path):
    """金额缺失或不是数字的一律不记——记忆里不留"看起来像价"的东西。"""
    database = tmp_path / 'memory.sqlite3'
    assert price_memory.record(_run(_row(unit_price=None)), TARGET, database_path=database) == 0
    assert price_memory.record(_run(_row(unit_price='2981.98')), TARGET, database_path=database) == 0
    assert price_memory.recall('SSW-420-2S', '上海博迅', database_path=database) == []


def test_the_switch_turns_it_off(tmp_path, monkeypatch):
    """``LAB_PRICE_MEMORY=off`` 时连库都不建（对照跑必须是干净的）。"""
    database = tmp_path / 'memory.sqlite3'
    monkeypatch.setenv('LAB_PRICE_MEMORY', 'off')
    assert price_memory.enabled() is False
    assert price_memory.record(_run(_row()), TARGET, database_path=database) == 0
    assert price_memory.recall('SSW-420-2S', '上海博迅', database_path=database) == []
    assert not database.exists()


def test_missing_database_recalls_nothing(tmp_path):
    assert price_memory.recall('SSW-420-2S', '上海博迅',
                               database_path=tmp_path / 'nope.sqlite3') == []


def test_an_empty_file_is_not_an_error(tmp_path):
    """库文件存在但没有建表（例如被手工 touch 过）：等价于"没有记忆"。"""
    database = tmp_path / 'empty.sqlite3'
    database.write_bytes(b'')
    assert price_memory.recall('SSW-420-2S', '上海博迅', database_path=database) == []


# ---------------------------------------------------------------------------
# 接线到价格车道：``_filter_recalled`` 的两件事
# ---------------------------------------------------------------------------

def test_the_same_transaction_is_not_carried_twice(tmp_path, monkeypatch):
    """当轮已经采信的那一笔，不许再以"记忆"的身份出现一次。

    记忆库的行没有 ``project_id``/``lot``/``item``，``_price_key`` 因此与当轮那笔
    不相等、``_merged_prices`` 去不掉重——这一步是唯一的防线。
    """
    from backend.services.procurement_research import _filter_recalled
    monkeypatch.setenv('LAB_PRICE_MEMORY_DB', str(tmp_path / 'memory.sqlite3'))
    row = _row()
    price_memory.record(_run(row), TARGET)
    assert _filter_recalled({'prices': [dict(row)]}, TARGET) == []


def test_a_price_from_an_earlier_round_is_brought_back(tmp_path, monkeypatch):
    """往轮采信的价，本轮没查到时要被带回来，且带 ``from_memory`` 标记。"""
    from backend.services.procurement_research import _filter_recalled
    monkeypatch.setenv('LAB_PRICE_MEMORY_DB', str(tmp_path / 'memory.sqlite3'))
    price_memory.record(_run(_row()), TARGET)
    recalled = _filter_recalled({'prices': []}, TARGET)
    assert len(recalled) == 1
    assert recalled[0]['from_memory'] is True
    assert recalled[0]['unit_price'] == 2981.98


def test_a_different_amount_from_the_same_page_is_still_brought_back(tmp_path, monkeypatch):
    """同一来源、不同金额：不是同一笔成交，必须照样带回（不能按 URL 一刀切）。"""
    from backend.services.procurement_research import _filter_recalled
    monkeypatch.setenv('LAB_PRICE_MEMORY_DB', str(tmp_path / 'memory.sqlite3'))
    price_memory.record(_run(_row()), TARGET)
    this_round = dict(_row(unit_price=3100.0))
    recalled = _filter_recalled({'prices': [this_round]}, TARGET)
    assert [row['unit_price'] for row in recalled] == [2981.98]
