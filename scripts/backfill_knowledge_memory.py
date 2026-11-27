# -*- coding: utf-8 -*-
"""把历史 run 台账灌进沉淀知识库（K1~K5）。

用途：验收 M8 的沉淀层——用 ``output/e2e-final/runs/`` 下几轮真实台账灌一次库，
看每一节各写进去多少、哪些没写进去以及为什么。**只读台账、只写知识库**：
不联网、不跑研究、不碰 ``data/price_memory.sqlite3``（那是另一条线上复用通道）。

用法（临时目录记得指到空间够的盘）::

    python scripts/backfill_knowledge_memory.py \
        --runs output\\e2e-final\\runs \
        --names m5-ui-check,b3-cleanbench-boxun-bj1cd-r1,b3-cleanbench-boxun-bj1cd-r3 \
        --database D:\\...\\knowledge_memory.sqlite3 \
        --report output/m8-ingest-report.json

同一份台账再跑一次就是幂等性验证：条数不变、``duplicates`` 计数把重复都接住。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import knowledge_memory  # noqa: E402
from backend.services.task_title import task_identity  # noqa: E402


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _run_payload(directory: Path):
    """这一轮的完整 run payload：优先收尾那一份，其次任一运行中的快照。"""
    for pattern in ('evidence/16-final-run.json', 'evidence/08-run-*.json',
                    'evidence/06-start-*.json', 'evidence/05-baseline-save.json'):
        for path in sorted(directory.glob(pattern)):
            payload = _load(path)
            if isinstance(payload, dict):
                return payload, path
    return None, None


def _category(directory: Path, run: dict) -> str:
    """品类沿用既有的 :func:`task_title.task_identity`（比较基准填了就用它，否则按场景／文档推断）。"""
    bundle = _load(directory / 'evidence/17-final-task.json') or {}
    task = bundle.get('task') or {}
    decision = bundle.get('final_decision') or {}
    documents = bundle.get('documents') or []
    candidates = bundle.get('candidates') or []
    identity = task_identity(task, candidates, documents, run.get('baseline') or {})
    return str(decision.get('category') or identity.get('category') or '').strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='把历史 run 台账灌进 K1~K5 沉淀知识库')
    parser.add_argument('--runs', default=str(ROOT / 'output' / 'e2e-final' / 'runs'),
                        help='run 台账根目录（每个子目录是一轮）')
    parser.add_argument('--names', default='', help='逗号分隔的轮次目录名；留空则取全部')
    parser.add_argument('--limit', type=int, default=0, help='轮次上限（0 = 不限制）')
    parser.add_argument('--database', default='', help='知识库路径（默认 data/knowledge_memory.sqlite3）')
    parser.add_argument('--report', default='', help='把本次灌库报告写到这个 JSON 文件')
    parser.add_argument('--recall', default='', help='灌完顺手取回这个型号的沉淀（看取回了什么）')
    parser.add_argument('--recall-category', default='', help='灌完顺手取回这个品类的参数表')
    args = parser.parse_args(argv)

    root = Path(args.runs)
    names = [name.strip() for name in args.names.split(',') if name.strip()]
    directories = [root / name for name in names] if names else sorted(
        path for path in root.iterdir() if path.is_dir())
    if args.limit:
        directories = directories[:args.limit]
    database = Path(args.database) if args.database else knowledge_memory.default_database_path()

    before = knowledge_memory.counts(database_path=database)
    report = {'database': str(database), 'runs_root': str(root), 'runs': [],
              'totals': {'written': Counter(), 'updated': Counter(), 'skipped': Counter(),
                         'failed': Counter(), 'duplicates_in_run': 0, 'already_stored': 0},
              'counts_before': before, 'counts_after': {}, 'missing': []}
    for directory in directories:
        run, path = _run_payload(directory)
        if run is None:
            report['missing'].append(directory.name)
            print(f'[skip] {directory.name}: 没有可读的 run payload')
            continue
        category = _category(directory, run)
        summary = knowledge_memory.record_run(run, category=category, run_dir=str(directory),
                                              database_path=database)
        entry = {'run': directory.name, 'payload': path.name, 'category': category,
                 'written': summary['written'], 'updated': summary['updated'],
                 'skipped': summary['skipped'],
                 'failed': summary['failed'],
                 'duplicates_in_run': summary['duplicates_in_run'],
                 'already_stored': summary['already_stored'],
                 'notes': summary.get('notes', [])}
        report['runs'].append(entry)
        for section, number in summary['written'].items():
            report['totals']['written'][section] += number
        for section, number in summary['updated'].items():
            report['totals']['updated'][section] += number
        for bucket in ('skipped', 'failed'):
            for reason, number in summary[bucket].items():
                report['totals'][bucket][reason] += number
        report['totals']['duplicates_in_run'] += summary['duplicates_in_run']
        report['totals']['already_stored'] += summary['already_stored']
        print(f"[ok] {directory.name}: 新增 {summary['written']} 更新 {summary['updated']} "
              f"跳过 {summary['skipped']} 失败 {summary['failed']} "
              f"本轮内重复 {summary['duplicates_in_run']} "
              f"以前存过 {summary['already_stored']} (品类 {category or '无'})")

    report['counts_after'] = knowledge_memory.counts(database_path=database)
    report['totals'] = {key: (dict(value) if isinstance(value, Counter) else value)
                        for key, value in report['totals'].items()}
    if args.recall:
        report['recall'] = knowledge_memory.recall_model(args.recall, database_path=database)
    if args.recall_category or args.recall:
        category = args.recall_category or report['runs'][-1]['category'] if report['runs'] else ''
        if category:
            report['recall_category'] = knowledge_memory.recall_category(category,
                                                                        database_path=database)
    print('\n总计 新增:', report['totals']['written'])
    print('总计 更新（以前存过、这一轮又见到）:', report['totals']['updated'])
    print('总计 跳过:', report['totals']['skipped'])
    print('总计 失败:', report['totals']['failed'])
    print('本轮内重复:', report['totals']['duplicates_in_run'],
          ' 以前存过（重复灌库）:', report['totals']['already_stored'])
    print('库内条数 前:', before, ' 后:', report['counts_after'])
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
        print('报告已写入', target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
