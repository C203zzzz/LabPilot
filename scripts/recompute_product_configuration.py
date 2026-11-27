"""老任务的价格配置离线重算：把库里已存的厂家参数按修正后的判据重算一遍。

背景
----
``prices[].configuration`` 的清洗加在 ``product_configuration()`` 里
（见 ``设计规范-配置清单清洗.md``），只作用于**新算出来**的那一份。库里早就
存着 ``configuration`` 的老任务走的是 ``hydrate_run`` → ``_fill``，而 ``_fill``
只补 ``configuration`` 为空／``'unknown'`` 的记录
（``backend/services/product_configuration.py:476``），已有值不覆盖——这是既有
设计：公告／合同附件真写了配置时，不能被厂家参数顶掉。所以
``task-013dcdcff7e6`` 页面上仍是 2400 字 / 74 行的旧文本。

这个脚本做的是**离线重算**：不重跑研究、不联网、不调模型，只用**库里已有的正文**
（``procurement_records.payload`` 里 ``run['sources'][i]['body_text']``，实测
``task-013dcdcff7e6`` 的那一条在这张表的 ``research-67255cfd82c840afa9558f70b6890193``
行里，16091 字）把 ``configuration_source`` 属于 ``SPEC_SOURCES``（厂家／产品页
参数）的记录重算一遍，写回同一张表。

边界（都是硬的）
----------------
* 只重算 ``configuration_source in SPEC_SOURCES`` 的记录。公告／合同附件自己写的
  配置（``configuration_source`` 不是 ``manufacturer_spec``／``product_page_spec``）
  一条都不动——那是这笔成交记录自己的事实。
* 正文只从库里读，**不发任何请求**；某条记录的正文已经不在库里（来源被清理、
  ``body_status != 'fetched'``）就跳过并计数，绝不重抓。
* 重算出来的页面与库里记的 ``configuration_url`` 不一致时**不写**并计数：那说明
  判据会换成另一页，属于换来源，不在"清洗同一份正文"的范围内。
* 幂等：正文没变就不写，第二遍 ``UPDATE`` 行数必须是 0。
* 可回滚：``--apply`` 之前把原值逐条写进备份 JSON，并把整库复制一份；
  ``--restore`` 用备份 JSON 逐条还原，``--restore-db`` 整份还原。

用法
----
    # 1) 先看（只读，不写任何东西）
    python scripts/recompute_product_configuration.py --db <库文件>

    # 2) 真写（先备份再写；连跑第二遍 UPDATE 行数应为 0）
    python scripts/recompute_product_configuration.py --db <库文件> --apply

    # 3) 回滚（用备份 JSON 逐条还原原值）
    python scripts/recompute_product_configuration.py --db <库文件> --restore <备份.json>

    # 4) 回滚（整份还原，最粗暴的一种）
    python scripts/recompute_product_configuration.py --db <库文件> --restore-db <备份.sqlite3> --yes

生产库 ``data/app.sqlite3`` 被显式拒绝，除非加 ``--allow-production``（不该用）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.product_configuration import (  # noqa: E402
    SPEC_SOURCES, product_configuration)

#: 只在本脚本里用的"脏内容"标记，用来在逐任务对照里印一行"清掉了没有"。
#: 都是上一单 ``设计规范`` 里点名的几类：表格展开、留言板按钮、推荐位、裸链接。
DIRT_MARKERS = ('[表格按列展开', '留言板', '发 送', '相关推荐', 'https://')

PRODUCTION_DB = ROOT / 'data' / 'app.sqlite3'


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def connect(path, read_only):
    if read_only:
        return sqlite3.connect('file:%s?mode=ro' % Path(path).as_posix(), uri=True)
    connection = sqlite3.connect(str(path))
    connection.execute('PRAGMA busy_timeout = 15000')
    return connection


def run_price_groups(run):
    """一个 run 里所有价格记录的分组：``(标签, 记录列表, 型号, 品牌)``。

    分组口径与 ``hydrate_run`` 逐字一致（``product_configuration.py:532-536``）：
    ``prices`` 用 ``baseline`` 的型号／品牌，每个候选用自己的。
    """
    baseline = run.get('baseline') or {}
    groups = [('prices', run.get('prices') or [], baseline.get('model'), baseline.get('brand'))]
    for index, candidate in enumerate(run.get('alternatives') or []):
        if isinstance(candidate, dict):
            groups.append(('alternatives[%d]' % index, candidate.get('prices') or [],
                           candidate.get('model'), candidate.get('brand')))
    return groups


def parameter_table(text):
    """正文里的「参数 | 值」行，做成 ``{参数名: 值}``（按第一个 `` | `` 切）。

    ``configuration`` 里参数表就是 ``render_table`` 吐出来的网格行
    （``web_tables.py:74``），所以 13 项参数的对照直接用这张表；不解释成参数的行
    （投影行 ``[表格按列展开 …]``、裸链接行）没有 `` | ``，自然不进表。
    """
    table = {}
    for line in str(text or '').splitlines():
        if ' | ' not in line:
            continue
        key, value = line.split(' | ', 1)
        table[key.strip()] = value.strip()
    return table


def parameter_diff(before, after):
    """逐条对照「参数 | 值」：值变了、丢了、多了，全部列出来。"""
    old, new = parameter_table(before), parameter_table(after)
    rows = []
    for key in list(old) + [key for key in new if key not in old]:
        if key in old and key in new:
            verdict = '一致' if old[key] == new[key] else '值变了'
        elif key in old:
            verdict = '丢了'
        else:
            verdict = '新增'
        rows.append({'key': key, 'before': old.get(key), 'after': new.get(key), 'verdict': verdict})
    return rows


def find_record(run, group, index):
    """按分组标签与下标回到那条价格记录；找不到返回 ``None``。"""
    for label, records, _model, _brand in run_price_groups(run):
        if label == group:
            if 0 <= index < len(records) and isinstance(records[index], dict):
                return records[index]
            return None
    return None


def source_stats(sources):
    """这批来源里正文的存量：总数／抓到正文的／真有正文文本的。

    这是"正文只从库里取"这句话的证据：重算只用 ``run['sources'][i]['body_text']``，
    它就在 ``procurement_records.payload`` 这份 JSON 里。
    """
    fetched = [item for item in sources
               if isinstance(item, dict) and item.get('body_status') == 'fetched']
    with_text = [item for item in fetched
                 if isinstance(item.get('body_text'), str) and item['body_text'].strip()]
    return len(sources), len(fetched), len(with_text)


def url_body_status(sources, url):
    """这条记录自己那页在库里的正文状态：``(body_status, 正文字数)``；没有这一页返回 ``None``。"""
    for item in sources:
        if isinstance(item, dict) and item.get('url') == url:
            body = item.get('body_text')
            return item.get('body_status'), len(body) if isinstance(body, str) else 0
    return None


def plan_run(connection, rowid, row_id, task_id, payload):
    """对一条 run 记录算出要改什么（只算不写）。返回 ``(run, 条目列表)``。

    只有**带价格分组的 run 行**才算 run：这张表里还有 222 条 ``kind='search_cache'``
    的缓存行也带 ``sources``，但它们没有 ``prices``／``alternatives``，价格配置
    自然也不在它们身上。
    """
    run = json.loads(payload)
    if not isinstance(run, dict) or 'sources' not in run:
        return None, []
    if 'prices' not in run and 'alternatives' not in run:
        return None, []
    sources = run.get('sources') or []
    stats = source_stats(sources)
    entries = []
    for group, records, model, brand in run_price_groups(run):
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            source = record.get('configuration_source')
            stored = record.get('configuration')
            base = {
                'rowid': rowid, 'run_id': row_id, 'task_id': task_id, 'group': group,
                'index': index, 'model': record.get('model'), 'brand': record.get('brand'),
                'configuration_source': source,
                'configuration_url': record.get('configuration_url'),
                'sources_total': stats[0], 'sources_fetched': stats[1],
                'sources_with_text': stats[2],
                'url_source': url_body_status(sources, record.get('configuration_url')),
            }
            if stored in (None, '', 'unknown'):
                # 空配置走读路径的 ``hydrate_run`` 就够了，这里不写库。
                base['action'] = 'skipped:empty_configuration'
                entries.append(base)
                continue
            if source not in SPEC_SOURCES:
                # 公告／合同附件自己写的配置——本脚本的边界之外，一条都不动。
                base['action'] = 'skipped:not_spec_source'
                base['before_chars'] = len(str(stored))
                base['before_lines'] = len(str(stored).splitlines())
                entries.append(base)
                continue
            stored = str(stored)
            spec = product_configuration(sources, model, brand)
            base.update({
                'before_chars': len(stored), 'before_lines': len(stored.splitlines()),
                'before_sha256': sha256_text(stored),
            })
            if not spec:
                base['action'] = 'skipped:no_source_text'
                entries.append(base)
                continue
            base.update({
                'before_chars': len(stored), 'before_lines': len(stored.splitlines()),
                'after_chars': len(spec['text']), 'after_lines': len(spec['text'].splitlines()),
                'before_sha256': sha256_text(stored), 'after_sha256': sha256_text(spec['text']),
                'dirt_before': [marker for marker in DIRT_MARKERS if marker in stored],
                'dirt_after': [marker for marker in DIRT_MARKERS if marker in spec['text']],
                'parameter_diff': parameter_diff(stored, spec['text']),
                'new_text': spec['text'],
            })
            if spec['url'] != record.get('configuration_url'):
                base['action'] = 'skipped:url_changed'
                base['recomputed_url'] = spec['url']
            elif spec['text'] == stored:
                base['action'] = 'unchanged'
            else:
                base['action'] = 'updated'
            entries.append(base)
    return run, entries


def guard_production(db, allow_production):
    """生产库默认拒跑：这个脚本会写 ``procurement_records``，写错库没有第二道闸。"""
    if Path(db).resolve() == PRODUCTION_DB.resolve() and not allow_production:
        raise SystemExit('拒绝在生产库上跑：%s（要真跑必须显式加 --allow-production）' % db)


def scan(db, allow_production=False):
    """只读扫一遍：``(逐 run 的计划, 行数统计)``。不写库。"""
    db = Path(db)
    guard_production(db, allow_production)
    connection = connect(db, read_only=True)
    stats = {'rows': 0, 'runs': 0}
    try:
        plans = []
        for rowid, row_id, task_id, payload in connection.execute(
                'SELECT rowid, id, task_id, payload FROM procurement_records ORDER BY rowid'):
            stats['rows'] += 1
            try:
                run, entries = plan_run(connection, rowid, row_id, task_id, payload)
            except json.JSONDecodeError:
                continue
            if run is None:
                continue
            stats['runs'] += 1
            plans.append((rowid, row_id, task_id, run, entries))
    finally:
        connection.close()
    return plans, stats


def summarize(plans, stats=None):
    counts = {'targets': 0, 'updated': 0, 'unchanged': 0, 'skipped:not_spec_source': 0,
              'skipped:empty_configuration': 0, 'skipped:no_source_text': 0,
              'skipped:url_changed': 0}
    for _rowid, _row_id, _task_id, _run, entries in plans:
        for entry in entries:
            action = entry['action']
            if action.startswith('skipped:'):
                counts[action] = counts.get(action, 0) + 1
                continue
            counts['targets'] += 1
            counts[action] = counts.get(action, 0) + 1
    counts['runs'] = len(plans)
    counts['rows'] = (stats or {}).get('rows', len(plans))
    counts['spec_records'] = (counts['targets'] + counts['skipped:no_source_text']
                              + counts['skipped:url_changed'])
    return counts


def write_report(plans, counts, db, mode, out):
    def say(*parts):
        out.write(' '.join(str(part) for part in parts) + '\n')

    say('=== 配置离线重算（%s）===' % mode)
    say('库：%s' % db)
    say('procurement_records 行数：%d（其中带价格分组的 run 行 %d，其余是搜索缓存等）'
        % (counts['rows'], counts['runs']))
    say('厂家参数来源（SPEC_SOURCES）且非空的配置记录：%d 条 → 待改 %d / 已是最新 %d / '
        '正文不在库里 %d / 重算换了页面 %d'
        % (counts['spec_records'], counts.get('updated', 0), counts.get('unchanged', 0),
           counts.get('skipped:no_source_text', 0), counts.get('skipped:url_changed', 0)))
    say('不动：配置为空 %d 条（留给读路径 hydrate_run）；公告／附件自带配置（非 SPEC_SOURCES）%d 条'
        % (counts.get('skipped:empty_configuration', 0), counts.get('skipped:not_spec_source', 0)))
    say('')
    for _rowid, row_id, task_id, _run, entries in plans:
        for entry in entries:
            if entry['action'] == 'skipped:empty_configuration':
                continue  # 空配置的普通价格记录，条数已在上面的汇总里，不逐条刷屏
            say('-- task=%s run=%s %s[%d] model=%s' % (
                entry['task_id'], entry['run_id'], entry['group'], entry['index'], entry.get('model')))
            say('   来源 %s  出处 %s' % (entry.get('configuration_source'), entry.get('configuration_url')))
            say('   库里来源 %d 条，抓到正文 %d 条，其中真有正文文本 %d 条；这条记录自己那页 '
                '(%s, %s 字)'
                % (entry['sources_total'], entry['sources_fetched'], entry['sources_with_text'],
                   (entry.get('url_source') or ('库里没有这一页', None))[0],
                   (entry.get('url_source') or (None, 0))[1]))
            if entry['action'] == 'skipped:not_spec_source':
                say('   动作：公告／附件自带配置，一条都不动（%d 字 / %d 行）'
                    % (entry['before_chars'], entry['before_lines']))
                say('')
                continue
            if entry['action'] == 'skipped:empty_configuration':
                say('   动作：配置本来就是空的，留给读路径的 hydrate_run，不写库')
                say('')
                continue
            if entry['action'] == 'skipped:no_source_text':
                say('   动作：正文已不在库里，跳过不重抓（改前 %d 字 / %d 行）'
                    % (entry['before_chars'], entry['before_lines']))
                say('')
                continue
            say('   改前 %d 字 / %d 行 → 改后 %d 字 / %d 行（动作 %s）'
                % (entry['before_chars'], entry['before_lines'],
                   entry['after_chars'], entry['after_lines'], entry['action']))
            say('   正文 sha256 改前 %s / 改后 %s'
                % (entry['before_sha256'][:16], entry['after_sha256'][:16]))
            say('   脏内容 改前 %s → 改后 %s'
                % (entry.get('dirt_before') or '无', entry.get('dirt_after') or '无'))
            if entry.get('recomputed_url'):
                say('   !! 重算选中的是另一页，未写：%s' % entry['recomputed_url'])
            diff = entry.get('parameter_diff') or []
            if diff:
                wrong = [row for row in diff if row['verdict'] != '一致']
                say('   「参数 | 值」逐条对照：共 %d 条，不一致 %d 条' % (len(diff), len(wrong)))
                for row in diff:
                    say('     %-16s 改前 %-36s 改后 %-36s %s'
                        % (row['key'], row['before'], row['after'], row['verdict']))
            say('')
    return out


def apply_plans(plans, db, backup_dir, allow_production=False):
    """把 ``updated`` 的记录写回库；写之前备份原值与整库。返回 ``(UPDATE 行数, 备份 JSON, 整库备份)``。"""
    db = Path(db)
    guard_production(db, allow_production)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir = Path(backup_dir) if backup_dir else db.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for rowid, row_id, task_id, _run, entries in plans:
        for entry in entries:
            if entry['action'] != 'updated':
                continue
            rows.append({
                'rowid': rowid, 'run_id': row_id, 'task_id': task_id,
                'group': entry['group'], 'index': entry['index'],
                'model': entry.get('model'), 'brand': entry.get('brand'),
                'configuration_source': entry.get('configuration_source'),
                'configuration_url': entry.get('configuration_url'),
                'configuration_chars': entry['before_chars'],
                'configuration_lines': entry['before_lines'],
                'configuration_sha256': entry['before_sha256'],
                'after_sha256': entry['after_sha256'],
                'configuration': None,
            })
    # 原值从库里重新读一遍（``entry`` 里只留了摘要，正文不留在内存计划里）。
    connection = connect(db, read_only=True)
    try:
        for row in rows:
            payload = connection.execute('SELECT payload FROM procurement_records WHERE rowid = ?',
                                         (row['rowid'],)).fetchone()[0]
            run = json.loads(payload)
            record = find_record(run, row['group'], row['index'])
            row['configuration'] = record['configuration']
    finally:
        connection.close()

    db_copy = backup_dir / ('%s-before-%s.sqlite3' % (db.stem, stamp))
    if not rows:
        # 幂等的第二遍会走到这里：没有一条要改，就不动库、也不浪费一份整库备份。
        return 0, None, None
    shutil.copy2(db, db_copy)
    backup = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'db': str(db), 'db_bytes': db.stat().st_size, 'db_sha256': sha256_file(db),
        'db_copy': str(db_copy),
        'note': 'configuration 的原值；--restore 用这份 JSON 逐条还原，'
                '--restore-db 用 db_copy 整份还原。',
        'rows': rows,
    }
    backup_path = backup_dir / ('%s-config-backup-%s.json' % (db.stem, stamp))
    backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')

    connection = connect(db, read_only=False)
    updates = 0
    try:
        for rowid, row_id, task_id, _planned_run, entries in plans:
            changed = [entry for entry in entries if entry['action'] == 'updated']
            if not changed:
                continue
            # 重新读一遍当前 payload 再改：这份库可能同时有别的研究在写（研究线程带租约，
            # 这个脚本不带），拿扫描时的内存副本写回会盖掉那期间的改动。
            payload = connection.execute('SELECT payload FROM procurement_records WHERE rowid = ?',
                                         (rowid,)).fetchone()
            if not payload:
                print('!! 跳过 rowid=%s：扫描之后这一行没了' % rowid)
                continue
            run = json.loads(payload[0])
            applied = 0
            for entry in changed:
                record = find_record(run, entry['group'], entry['index'])
                if not isinstance(record, dict):
                    print('!! 跳过 %s %s[%s]：扫描之后这条记录没了'
                          % (row_id, entry['group'], entry['index']))
                    continue
                current = record.get('configuration')
                if sha256_text(str(current)) != entry['before_sha256']:
                    print('!! 跳过 %s %s[%s]：扫描之后这一格已经变过（%d 字），不覆盖'
                          % (row_id, entry['group'], entry['index'],
                             len(str(current)) if current is not None else 0))
                    continue
                record['configuration'] = entry['new_text']
                applied += 1
            if not applied:
                continue
            connection.execute('UPDATE procurement_records SET payload = ? WHERE rowid = ?',
                               (json.dumps(run, ensure_ascii=False), rowid))
            updates += 1
        connection.commit()
    finally:
        connection.close()
    return updates, backup_path, db_copy


def restore(db, backup_path):
    """用备份 JSON 逐条还原 ``configuration`` 原值。返回还原条数。"""
    backup = json.loads(Path(backup_path).read_text(encoding='utf-8'))
    connection = connect(db, read_only=False)
    restored = 0
    try:
        for row in backup.get('rows') or []:
            payload = connection.execute('SELECT payload FROM procurement_records WHERE rowid = ?',
                                         (row['rowid'],)).fetchone()
            if not payload:
                print('跳过：库里已经没有 rowid=%s' % row['rowid'])
                continue
            run = json.loads(payload[0])
            record = find_record(run, row['group'], row['index'])
            if not isinstance(record, dict):
                print('跳过：rowid=%s 的 %s[%s] 已不存在' % (row['rowid'], row['group'], row['index']))
                continue
            if record.get('configuration_url') != row.get('configuration_url'):
                print('跳过：rowid=%s 的出处已经变了（%s → %s）'
                      % (row['rowid'], row.get('configuration_url'), record.get('configuration_url')))
                continue
            record['configuration'] = row['configuration']
            connection.execute('UPDATE procurement_records SET payload = ? WHERE rowid = ?',
                               (json.dumps(run, ensure_ascii=False), row['rowid']))
            restored += 1
        connection.commit()
    finally:
        connection.close()
    return restored


def restore_db(db, backup_db, confirmed):
    if not confirmed:
        raise SystemExit('整份还原会用备份文件覆盖目标库，必须加 --yes')
    shutil.copy2(backup_db, db)
    return 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001 - 只为控制台好看，失败不影响逻辑
        pass
    parser = argparse.ArgumentParser(description='老任务 prices[].configuration 离线重算')
    parser.add_argument('--db', required=True, help='库文件（默认只读；--apply 才写）')
    parser.add_argument('--apply', action='store_true', help='真写库（写前备份原值与整库）')
    parser.add_argument('--backup-dir', help='备份目录（默认与库同目录）')
    parser.add_argument('--report', help='把逐任务对照写到这个文件（同时打到标准输出）')
    parser.add_argument('--restore', help='用备份 JSON 逐条还原')
    parser.add_argument('--restore-db', help='用整库备份覆盖目标库（要 --yes）')
    parser.add_argument('--yes', action='store_true', help='配合 --restore-db')
    parser.add_argument('--allow-production', action='store_true',
                        help='允许在 data/app.sqlite3 上跑（不该用）')
    args = parser.parse_args(argv)

    if args.restore:
        guard_production(args.db, args.allow_production)
        restored = restore(args.db, args.restore)
        print('已还原 %d 条 configuration 原值' % restored)
        return 0
    if args.restore_db:
        guard_production(args.db, args.allow_production)
        restore_db(args.db, args.restore_db, args.yes)
        print('已用 %s 整份覆盖 %s' % (args.restore_db, args.db))
        return 0

    plans, stats = scan(args.db, allow_production=args.allow_production)
    counts = summarize(plans, stats)
    buffer = io.StringIO()
    write_report(plans, counts, args.db, 'APPLY' if args.apply else 'DRY-RUN', buffer)
    text = buffer.getvalue()
    print(text, end='')

    updates = 0
    if args.apply:
        updates, backup_path, db_copy = apply_plans(plans, args.db, args.backup_dir,
                                                    allow_production=args.allow_production)
        print('---')
        print('UPDATE 行数 = %d（按 run 行计；这一遍改了 %d 条价格记录；第二遍应为 0）'
              % (updates, counts.get('updated', 0)))
        if backup_path:
            print('备份 JSON：%s' % backup_path)
            print('整库备份：%s' % db_copy)
            print('回滚：python scripts/recompute_product_configuration.py --db "%s" --restore "%s"'
                  % (args.db, backup_path))
            text += ('\n---\nUPDATE 行数 = %d（按 run 行计，改了 %d 条价格记录）\n'
                     '备份 JSON：%s\n整库备份：%s\n'
                     % (updates, counts.get('updated', 0), backup_path, db_copy))
        else:
            print('没有一条要改，未写库、也未生成新的备份（备份 JSON：无）')
            text += '\n---\nUPDATE 行数 = 0（没有一条要改，未写库、未生成新备份）\n'
    else:
        print('---')
        print('DRY-RUN：没有写库。加 --apply 才写。')

    if args.report:
        Path(args.report).write_text(text, encoding='utf-8')
        print('对照写到 %s' % args.report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
