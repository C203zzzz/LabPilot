"""离线重算脚本（``scripts/recompute_product_configuration.py``）的行为钉子。

这个脚本要做的是一件有风险的事：**直接改库里已经存下来的价格配置**。所以这几条
必须钉住：

* 只重算 ``configuration_source`` 是厂家／产品页参数（``SPEC_SOURCES``）的记录；
* **公告／附件自己写的配置一个字都不动**——这是既有设计（``_fill`` 的注释、
  ``设计规范`` 第六节），也是这个脚本最容易踩坏的地方；
* 正文只从库里取，取不到就跳过，不重抓；
* 幂等（第二遍 0 条 UPDATE）；
* 改前原值留得住、能逐条还原；
* 重算后的配置比旧的那份干净，而且「参数 | 值」一条不少、值一个不改。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from backend.services.procurement_research import configuration_regions
from scripts.recompute_product_configuration import (
    apply_plans, parameter_table, restore, scan, summarize)

MODEL = 'XKA-2200'
URL = 'https://www.xiangyilxj.com/product/info_itemid_952.html'

#: 合成的一页厂家产品页正文：一张参数表（同样被投影输出两遍）+ 产品页框架内容
#: （留言板按钮、推荐位产品名 + 裸链接）。形状抄自真实那一页（``设计规范`` 第一节），
#: 只是把填充文字拉长到 12000 字以上，好让 ``configuration_regions`` 真的切出
#: 一个 2400 字的窗口——老的那份 ``configuration`` 就是这么来的。
_HEAD = """技术参数
型号 | XKA-2200
最高转速 | 4500r/min
转子容量 | (0.25～1ml)×12
[表格按列展开 行2列2] XKA-2200 最高转速 4500r/min
[表格按列展开 行3列2] XKA-2200 转子容量 (0.25～1ml)×12
留言板
发 送
相关推荐
H300R大容量高速冷冻离心机
[https://www.xiangyilxj.com/product/info_itemid_2261.html]
CHT200R大容量高速台式冷冻离心机
[https://www.xiangyilxj.com/product/info_itemid_2260.html]
RM32R 低速冷冻离心机
[https://www.xiangyilxj.com/product/info_itemid_2241.html]
*所有产品都可根据客户要求定制、设计、生产。
"""


def body():
    filler = '\n'.join('页面填充文字第 %d 行，与参数无关。' % index for index in range(400))
    return _HEAD + filler + '\n' + filler


def stored_dirty_configuration():
    """老代码存下来的那份：``configuration_regions`` 的窗口原文，未经清洗。"""
    regions = configuration_regions(body(), MODEL)
    return '\n'.join(region['text'] for region in regions).strip()


def write_db(path):
    """造一个只有 ``procurement_records`` 的最小库，四种记录各来一条。"""
    dirty = stored_dirty_configuration()
    assert '[表格按列展开' in dirty and '留言板' in dirty, '样本必须先真的是脏的'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE procurement_records (id TEXT, task_id TEXT, payload TEXT)')
    run = {
        'id': 'research-synthetic', 'task_id': 'task-synthetic', 'kind': 'research',
        'sources': [{'url': URL, 'body_status': 'fetched', 'body_text': body(),
                     'source_type': 'manufacturer_website'}],
        'baseline': {'model': MODEL, 'brand': '湘仪'},
        'prices': [
            {'model': MODEL, 'configuration': dirty, 'configuration_source': 'manufacturer_spec',
             'configuration_url': URL},
            {'model': MODEL, 'configuration': '公告写的配置：转子 HLA/SERO 各一套。',
             'configuration_source': None, 'configuration_url': None},
            {'model': MODEL, 'configuration': 'unknown', 'configuration_source': None},
            {'model': MODEL, 'configuration': '另一个第三方页面的旧配置',
             'configuration_source': 'product_page_spec',
             'configuration_url': 'https://other.example.com/x'},
        ],
        'alternatives': [
            {'model': MODEL, 'brand': '湘仪', 'prices': [
                {'model': MODEL, 'configuration': dirty,
                 'configuration_source': 'manufacturer_spec', 'configuration_url': URL}]},
            # 这条候选的型号在库里一个可用来源都没有：正文取不到，只能跳过。
            {'model': 'ZZZ-1', 'brand': '别的', 'prices': [
                {'model': 'ZZZ-1', 'configuration': '第三方页面的旧配置',
                 'configuration_source': 'product_page_spec',
                 'configuration_url': 'https://gone.example.com/p'}]},
        ],
    }
    connection.execute('INSERT INTO procurement_records(id, task_id, payload) VALUES (?, ?, ?)',
                       (run['id'], run['task_id'], json.dumps(run, ensure_ascii=False)))
    connection.commit()
    connection.close()
    return dirty


def load_run(path):
    connection = sqlite3.connect(path)
    payload = connection.execute('SELECT payload FROM procurement_records').fetchone()[0]
    connection.close()
    return json.loads(payload)


def actions(plans):
    return [entry['action'] for _rowid, _row_id, _task_id, _run, entries in plans
            for entry in entries]


def test_only_spec_source_records_are_recomputed(tmp_path):
    db = tmp_path / 'acceptance.sqlite3'
    dirty = write_db(db)
    plans, stats = scan(db)
    counts = summarize(plans, stats)
    assert stats == {'rows': 1, 'runs': 1}
    # 要重算的记录：2 条 manufacturer_spec（基准 1 条 + 候选 1 条）+ 1 条换页 + 1 条没正文。
    assert counts['spec_records'] == 4
    assert counts['updated'] == 2
    # 公告自带配置与空配置一条都不算在重算范围里。
    assert counts['skipped:empty_configuration'] == 1
    assert counts['skipped:not_spec_source'] == 1

    updates, backup_path, db_copy = apply_plans(plans, db, tmp_path / 'backup')
    assert updates == 1, '两条记录在同一个 run 行里：UPDATE 一条 SQL 就把它们一起写了'
    assert counts['updated'] == 2, '但改的是 2 条价格记录'
    run = load_run(db)
    after = run['prices'][0]['configuration']
    assert after != dirty and len(after) < len(dirty)
    for marker in ('[表格按列展开', '留言板', '发 送', '相关推荐'):
        assert marker not in after, marker
    # 参数一条不少、值一个不改：清洗只删重复与框架。
    before_table, after_table = parameter_table(dirty), parameter_table(after)
    assert before_table, '样本里必须有参数行'
    assert all(after_table.get(key) == value for key, value in before_table.items())

    # 公告／附件自己写的配置：逐字不动。
    assert run['prices'][1]['configuration'] == '公告写的配置：转子 HLA/SERO 各一套。'
    # 'unknown' 占位留给读路径的 hydrate_run。
    assert run['prices'][2]['configuration'] == 'unknown'
    # 重算会换成另一页的那条：不写。
    assert run['prices'][3]['configuration'] == '另一个第三方页面的旧配置'
    # 正文已不在库里的那条（候选 ZZZ-1）：跳过、不重抓。
    assert run['alternatives'][1]['prices'][0]['configuration'] == '第三方页面的旧配置'
    # 候选里的那一条也重算了。
    assert run['alternatives'][0]['prices'][0]['configuration'] == after
    assert backup_path.exists()


def test_the_second_pass_updates_nothing(tmp_path):
    db = tmp_path / 'acceptance.sqlite3'
    write_db(db)
    plans, stats = scan(db)
    assert apply_plans(plans, db, tmp_path / 'backup')[0] == 1
    again, stats = scan(db)
    assert [entry['action'] for _r, _i, _t, _run, entries in again for entry in entries
            if entry['action'] in {'updated', 'unchanged'}] == ['unchanged', 'unchanged']
    updates, backup_path, db_copy = apply_plans(again, db, tmp_path / 'backup')
    assert updates == 0
    assert backup_path is None and db_copy is None, '没有要改的就不该再复制整库'


def test_restore_puts_every_original_value_back(tmp_path):
    db = tmp_path / 'acceptance.sqlite3'
    dirty = write_db(db)
    plans, _stats = scan(db)
    _updates, backup_path, _copy = apply_plans(plans, db, tmp_path / 'backup')
    assert load_run(db)['prices'][0]['configuration'] != dirty
    assert restore(db, backup_path) == 2
    run = load_run(db)
    assert run['prices'][0]['configuration'] == dirty
    assert run['alternatives'][0]['prices'][0]['configuration'] == dirty
    backup = json.loads(backup_path.read_text(encoding='utf-8'))
    assert {row['group'] for row in backup['rows']} == {'prices', 'alternatives[0]'}
    assert all(row['configuration'] for row in backup['rows']), '备份里必须是原值全文'


def test_the_production_database_is_refused(tmp_path):
    from scripts.recompute_product_configuration import PRODUCTION_DB
    with pytest.raises(SystemExit):
        scan(PRODUCTION_DB)
    with pytest.raises(SystemExit):
        apply_plans([], PRODUCTION_DB, tmp_path)


def test_a_value_changed_after_the_scan_is_not_overwritten(tmp_path):
    """扫描之后这一格被别人改过（这份库同时有别的研究在写）：跳过，不覆盖。"""
    db = tmp_path / 'acceptance.sqlite3'
    write_db(db)
    plans, _stats = scan(db)
    # 模拟"扫描之后研究线程又写了这一格"。
    connection = sqlite3.connect(db)
    payload = connection.execute('SELECT payload FROM procurement_records').fetchone()[0]
    run = json.loads(payload)
    run['prices'][0]['configuration'] = '扫描之后被别的写入改过的配置'
    connection.execute('UPDATE procurement_records SET payload = ?',
                       (json.dumps(run, ensure_ascii=False),))
    connection.commit()
    connection.close()

    updates, _backup, _copy = apply_plans(plans, db, tmp_path / 'backup')
    assert updates == 1, '候选里那一条没被碰过，仍然要写'
    run = load_run(db)
    assert run['prices'][0]['configuration'] == '扫描之后被别的写入改过的配置'
    assert '[表格按列展开' not in run['alternatives'][0]['prices'][0]['configuration']


def test_skipped_records_are_counted_with_a_reason(tmp_path):
    db = tmp_path / 'acceptance.sqlite3'
    write_db(db)
    plans, stats = scan(db)
    counts = summarize(plans, stats)
    assert counts['skipped:no_source_text'] == 1, '正文不在库里的那条要计数'
    assert counts['skipped:url_changed'] == 1, '重算换页的那条要计数'
    skipped = [entry for _r, _i, _t, _run, entries in plans for entry in entries
               if entry['action'] == 'skipped:no_source_text'][0]
    assert skipped['model'] == 'ZZZ-1'
    assert skipped['sources_fetched'] == 1 and skipped['sources_with_text'] == 1
    assert skipped['url_source'] is None, '库里没有那条 URL 的正文'