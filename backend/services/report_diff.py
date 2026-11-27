"""M6报告版本对比：这一版报告和它的上一版差在哪。

背景：用户回填销售回复后会另出**一版**报告（``procurement_research.supplier_update``
在比较基准真的变了时重建一轮 run，新版报告把 ``parent_report_id`` 指向上一版）。
但报告库里两版长得一模一样，填完看不出"改变了什么"，这个动作就等于没意义。

这份文件只做**读**：拿两份已经存在的报告（页面口径的那一份）逐条比，不重算任何判定、
不写库、不碰研究流程（``procurement_research`` 一行不改）。

读法的四条口径：

1. 对比的是**渲染结果**——报告 ``sections`` 里的逐条 ``label``/``text``（含节摘要）与
   顶层总述，不是内部快照字段。页面上看得见的东西变了，才算"这一版变了"。
2. 一条差异只报一次，按**条目所属的那一节**归进价格/配置/缺口三类之一；归不进三类的
   （销售回复更新节等）单独放 ``other``，不许静默丢（``设计规范`` §3、§5）。
3. 「比较基准」节里那些"还没拿到的条件"归**缺口**，不归配置：这句话从"本轮没有向销售
   询问这一项"改成"本轮销售回复没有涉及这一项"，说的是缺口状态变了，不是配置变好了。
   配置类只剩下**真的补上了值**的那些项——读者要的"配置补了什么"就是它们。
4. 两侧都有、文本逐字相同的条目算"没变"，把条数一并报出来：读者要能确认
   **其余零变化**，而不是只能相信"列出来的这几条"。
"""
from __future__ import annotations

from typing import Any

#: 节 → 类别。价格与核价判定各占一节；「比较基准」节里只有报价那一条属于价格，
#: 其余（配置、税费、运费、保修……）是配置/服务条件。缺口单指「证据缺口」节。
PRICE_SECTIONS = ('decision', 'prices', 'price_rejections', 'alternative_prices')
CONFIGURATION_SECTIONS = ('baseline', 'product_fit', 'comparison_details', 'alternatives', 'supplement')
GAP_SECTIONS = ('gaps',)
#: 「比较基准」节里这一条讲的是钱，不是配置。
PRICE_BASELINE_LABELS = ('销售单价',)

CATEGORIES = (
    ('price', '价格'),
    ('configuration', '配置'),
    ('gaps', '缺口'),
)

#: 「这一项还是空的」在报告里的三种说法（``procurement_research.missing_information_state``
#: 逐字产出）。两侧都是这种说法时，这条变化讲的是**缺口状态**，归缺口类。
#: 这是唯一一处按文案分类的地方；`tests/test_m6_report_diff.py` 里有一条测试盯着它，
#: 产品改了措辞、这里没跟上，那条测试先红。
GAP_STATE_PREFIXES = (
    '销售已回复：资料中未提供',
    '本轮销售回复没有涉及这一项',
    '本轮没有向销售询问这一项',
)

#: 节摘要也渲染在页面上，所以也参与对比；用这个固定 label 与同节的条目区分开
#: （节标题可能为空，所以 label 里不带标题）。
SECTION_SUMMARY_LABEL = '本节摘要'
OVERVIEW_SECTION = 'overview'


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _readable(value: Any) -> str:
    """一条可能不是字符串的内容（总述里的 blockers 是对象）取成能读的一句话。"""
    if isinstance(value, dict):
        for key in ('text', 'message', 'note', 'reason', 'label'):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return _text(value)


def _gap_state(text: str) -> bool:
    return any(_text(text).startswith(prefix) for prefix in GAP_STATE_PREFIXES)


def _category(section_key: str, label: str) -> str:
    if section_key in PRICE_SECTIONS:
        return 'price'
    if section_key in GAP_SECTIONS:
        return 'gaps'
    if section_key in CONFIGURATION_SECTIONS:
        return 'price' if label in PRICE_BASELINE_LABELS else 'configuration'
    return 'other'


def _missing_conditions(run: dict) -> list[str]:
    """比较基准里**还是空着**的那些条件（采购员嘴里的"缺口少了几项"）。

    判据用权威字段（``baseline`` 里的值本身），不是去匹配报告里那几句措辞——报告
    那几句正是从这些字段派生出来的，拿措辞反推会在换了说法之后悄悄失灵。
    字段清单用 ``SALES_FIELDS``：与「比较基准」节逐条列出的那一份是同一份。
    """
    from backend.services.procurement_research import SALES_FIELDS

    baseline = (run or {}).get('baseline') or {}
    missing = [label for key, label in SALES_FIELDS if baseline.get(key) in (None, '', [])]
    if baseline.get('supplier_price') in (None, ''):
        missing.append('销售单价')
    if not str(baseline.get('configuration') or '').strip():
        missing.append('配置')
    return missing


def _flatten(report: dict) -> list[dict]:
    """把一份报告的 ``sections`` 摊平成有顺序的条目表（节键 + label + 文本）。

    同一节里 label 可以重复（比如逐项比较里的型号行），所以后面按
    「(节键, label) 分组 + 组内出现顺序」对齐，不按全局下标——节是插入进去的，
    下标会随着新增一节整体位移，那样会把没变的条目报成"变了"。
    """
    entries: list[dict] = []
    for section in report.get('sections') or []:
        if not isinstance(section, dict):
            continue
        key = _text(section.get('key') or section.get('title')).strip()
        title = _text(section.get('title')).strip()
        summary = _text(section.get('summary')).strip()
        if summary:
            entries.append({'section': key, 'section_title': title, 'label': SECTION_SUMMARY_LABEL, 'text': summary})
        for item in section.get('items') or []:
            if not isinstance(item, dict):
                continue
            entries.append({'section': key, 'section_title': title,
                            'label': _text(item.get('label')).strip(), 'text': _text(item.get('text')).strip()})
    return entries


def _overview_entries(report: dict) -> list[dict]:
    """顶层总述也渲染在页面最上面，所以也参加对比；但它是模型写的、结构比 sections 松，
    逐块摊平成条目，好让对比说清"哪一句改了"，而不是把一个 dict 摆给读者看。"""
    summary = report.get('summary')
    if isinstance(summary, str):
        return [{'section': OVERVIEW_SECTION, 'section_title': '总述', 'label': '总述', 'text': summary.strip()}] if summary.strip() else []
    if not isinstance(summary, dict):
        return []
    entries: list[dict] = []
    for key, label in (('headline', '总述结论'), ('narrative', '总述说明')):
        text = summary.get(key)
        if isinstance(text, str) and text.strip():
            entries.append({'section': OVERVIEW_SECTION, 'section_title': '总述', 'label': label, 'text': text.strip()})
    for index, fact in enumerate(summary.get('facts') or [], 1):
        label = f"要点 · {fact.get('label')}" if isinstance(fact, dict) and fact.get('label') else f'要点 {index}'
        entries.append({'section': OVERVIEW_SECTION, 'section_title': '总述', 'label': label,
                        'text': _readable(fact.get('value') if isinstance(fact, dict) else fact)})
    for index, blocker in enumerate(summary.get('blockers') or [], 1):
        entries.append({'section': OVERVIEW_SECTION, 'section_title': '总述', 'label': f'未完成事项 {index}',
                        'text': _readable(blocker)})
    return entries


def _group(entries: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        grouped.setdefault((entry['section'], entry['label']), []).append(entry)
    return grouped


def _pairs(before: list[dict], after: list[dict]):
    """按 (节键, label) 分组、组内按出现顺序对齐；返回 (旧条目, 新条目) 对。"""
    old, new = _group(before), _group(after)
    ordered = list(new) + [key for key in old if key not in new]
    for key in ordered:
        left, right = old.get(key, []), new.get(key, [])
        for index in range(max(len(left), len(right))):
            yield (left[index] if index < len(left) else None,
                   right[index] if index < len(right) else None)


def _iter_changes(before: list[dict], after: list[dict]):
    """逐条对齐两份条目表，产出 changed / added / removed / same 四种结果。"""
    for left, right in _pairs(before, after):
        sample = right or left
        if left is not None and right is not None:
            if left['text'] == right['text']:
                yield {'change': 'same', **{key: sample[key] for key in ('section', 'section_title', 'label')}}
                continue
            change = {'change': 'changed', 'before': left['text'], 'after': right['text']}
        elif right is not None:
            change = {'change': 'added', 'before': None, 'after': right['text']}
        else:
            change = {'change': 'removed', 'before': left['text'], 'after': None}
        yield {**{key: sample[key] for key in ('section', 'section_title', 'label')}, **change}


def compare_reports(before_report: dict, after_report: dict,
                    before_run: dict | None = None, after_run: dict | None = None) -> dict:
    """两份报告（页面口径）逐条比，返回三类差异 + 其它变化 + 未变计数。

    纯函数：只读入参，不碰库、不重算判定。``before_run``/``after_run`` 是两版各自的
    研究快照，给得出时才多报一条「仍缺的条件」（缺口类的头一条）；给不出就不报，
    不拿报告措辞去猜。
    """
    before, after = before_report or {}, after_report or {}
    buckets: dict[str, list[dict]] = {key: [] for key, _ in CATEGORIES}
    other: list[dict] = []

    if before_run is not None and after_run is not None:
        was, now = _missing_conditions(before_run), _missing_conditions(after_run)
        if was != now:
            filled = [name for name in was if name not in now]
            appeared = [name for name in now if name not in was]
            tail = ''
            if filled:
                tail += '；这一版补上了：' + '、'.join(filled)
            if appeared:
                tail += '；这一版新出现的缺口：' + '、'.join(appeared)
            buckets['gaps'].append({
                'section': 'baseline', 'section_title': '比较基准', 'label': '仍缺的条件',
                'change': 'changed',
                'before': f'上一版仍缺 {len(was)} 项（{"、".join(was) or "无"}）',
                'after': f'本版仍缺 {len(now)} 项（{"、".join(now) or "无"}）{tail}',
            })

    unchanged = 0
    for change in _iter_changes(_flatten(before), _flatten(after)):
        if change['change'] == 'same':
            unchanged += 1
            continue
        # 两侧都还在说"这一项没有"：变化的是缺口的状态（没问 → 问了说没有），归缺口；
        # 一旦有一侧给了值，那就是配置补上了，归配置。
        if change['change'] == 'changed' and _gap_state(change['before']) and _gap_state(change['after']):
            buckets['gaps'].append(change)
            continue
        bucket = _category(change['section'], change['label'])
        (buckets[bucket] if bucket in buckets else other).append(change)

    # 总述（模型写的那段话）单列：它照样是渲染结果、照样列出来，但不算进三类事实差异，
    # 免得读者把一次改述读成"结论变了"。
    notes = [change for change in _iter_changes(_overview_entries(before), _overview_entries(after))
             if change['change'] != 'same']

    categories = [{'key': key, 'title': title, 'items': buckets[key]} for key, title in CATEGORIES]
    return {
        'categories': categories,
        'other': other,
        'notes': notes,
        'unchanged': {'items': unchanged},
        'changed_total': sum(len(category['items']) for category in categories) + len(other),
    }


def report_diff(repo, report_id: str) -> dict:
    """取库里这一版报告与它 ``parent_report_id`` 指的上一版，给出差异。

    两侧都用 ``sanitize_report`` 清一遍：页面上看到的就是清完的那一份，拿原件比对会
    把读者看不见的差别算成变化（详情见 ``procurement_research.sanitize_report``）。
    """
    from backend.services.procurement_research import sanitize_report

    def load(identifier):
        record = repo.get('reports', identifier)
        if not isinstance(record, dict) or not isinstance(record.get('report'), dict):
            return None, {}, None
        return (sanitize_report(record['report'], record.get('snapshot') or {}),
                record.get('snapshot') or {}, record)

    current_report, current_run, _ = load(report_id)
    if current_report is None:
        return {'available': False, 'reason': 'report_not_found',
                'message': '这份报告不存在，可能已被删除，请刷新报告库。'}
    parent_id = _text(current_report.get('parent_report_id')).strip()
    if not parent_id:
        return {'available': False, 'reason': 'first_version', 'report_id': report_id,
                'message': '这是这份报告的第一版：还没有上一版可以对比。回填销售回复并另出版本后，这里会列出两版差异。'}
    previous_report, previous_run, _ = load(parent_id)
    if previous_report is None:
        return {'available': False, 'reason': 'previous_missing', 'report_id': report_id,
                'previous_report_id': parent_id,
                'message': '上一版报告已被删除，无法对比。'}
    result = compare_reports(previous_report, current_report,
                             previous_run or None, current_run or None)
    result.update({
        'available': True,
        'report_id': report_id,
        'previous_report_id': parent_id,
        'previous_created_at': previous_report.get('created_at'),
        'created_at': current_report.get('created_at'),
    })
    return result


__all__ = ['CATEGORIES', 'compare_reports', 'report_diff']
