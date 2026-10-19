"""采购要求 = 采购方开出的门槛，与产品参数分开的一条独立证据。

招标文件里的「▲5.最高转速：≥4500r/min。」是采购方对**投标产品**提出的
要求，不是 XKA-2200 的标称参数。把它写进 ``configuration``（那是产品配置），
用户看到的就是「XKA-2200 最高转速 4500r/min」——一条凭空捏造的产品参数。
所以这里产出的是与 ``configuration`` 并列、但分开的证据类型
``tender_requirement``，并把「采购清单表 → 技术参数表 → 中标公告」这条绑定
链的每一步位置都记下来，任何一条要求都能回答「你凭什么把它挂到这台设备上」。

证据等级低于产品实测参数：中标的型号按定义满足了招标文件，但那是符合性
审查推导出的**制度推断**，不是任何人对这台机器测出来的数。字段
``evidence_level='inferred_compliance'`` 就是这个意思。

歧义一律降级：同一个品类出现在多个包里时不绑定设备，只作为项目级要求展示
（``project_level``）。这次的文档四包互不重叠、绑得上，但代码不假设总是这样。
"""
import re

#: 证据类型与等级。前端展示时不要把这两个内部取值直接印出来。
EVIDENCE_TYPE = 'tender_requirement'
EVIDENCE_LEVEL = 'inferred_compliance'
NATURE = ('采购方在招标文件中开出的门槛与要求。条目文字是采购方的要求，'
          '不是该型号的产品标称参数；「中标人须满足」由符合性审查推导，'
          '不是可引用的实测证据。')

_PACKAGE_BUDGET = re.compile(r'^\s*(\d+)\s*包\s*预算金额\s*[:：]\s*(.+?)\s*$')
_LIST_HEADER = re.compile(r'设备名称.*预算单价|预算单价.*设备名称')
_SECTION_HEADING = re.compile(r'^\s*(\d+)\s*包\s*技术参数要求\s*$')
_DEVICE_HEADING = re.compile(r'^\s*(?P<prefix>[一二三四五六七八九十]+\s*、)\s*(?P<name>\S.*?)\s*$')
_LIST_ITEM_PREFIX = re.compile(r'^\s*[（(]')
_CLAUSE = re.compile(r'^\s*(?P<key>[▲★])?\s*(?P<index>\d+)\s*[.．、]\s*(?P<text>\S.*?)\s*$')
_CHAPTER = re.compile(r'^\s*第[一二三四五六七八九十]+章')
_AWARD_PACKAGE = re.compile(r'标段名称\s*[:：].*?第\s*(\d+)\s*包')
_AWARD_FIELD = re.compile(r'^\s*(名称|品牌|规格型号|数量|单价\s*[（(]\s*元\s*[）)])\s*[:：]\s*(.*?)\s*$')


def _normalise(name):
    """设备名的规范化：去掉「（核心产品）」这类括注和空白，只用于配对。"""
    return re.sub(r'[（(][^）)]*[）)]', '', name or '').strip()


def _package_order(package):
    match = re.match(r'(\d+)', package or '')
    return (int(match.group(1)) if match else 0, package or '')


def _cells(line):
    return [cell.strip() for cell in line.split('|')]


def _clause(line, number):
    """把一行参数读成一条要求；不是要求行就返回 ``None``。"""
    match = _CLAUSE.match(line)
    if not match or not match.group('text').strip():
        return None
    return {'index': int(match.group('index')), 'key': bool(match.group('key')),
            'clause': ('▲' if match.group('key') else '') + match.group('index'),
            'text': match.group('text').strip(), 'line': number}


def device_list(lines, tables=None):
    """采购设备清单：包 → 该包的设备。绑定链的第 1 步。

    同一份招标文件里常有**两份**内容相同的清单（第一章招标公告的「5.采购
    需求」和第四章的「（一）采购设备清单」）。有解析报告时取第四章那一份：
    它和参数表同属采购需求章节，位置可以直接引用；没有报告时用第一份。
    """
    preferred = [table for table in tables or [] if '采购设备清单' in (table.get('heading') or '')]
    if preferred:
        start, heading = preferred[0]['start_line'], preferred[0].get('heading')
    else:
        start = next((number for number, line in enumerate(lines) if _LIST_HEADER.search(line)), None)
        heading = None
    if start is None:
        return {}, None
    packages, current, misses = {}, None, 0
    for number in range(start + 1, len(lines)):
        line = lines[number]
        budget = _PACKAGE_BUDGET.match(line)
        if budget:
            current = budget.group(1) + '包'
            packages[current] = {'budget_text': budget.group(2), 'budget_line': number, 'devices': []}
            misses = 0
            continue
        if _SECTION_HEADING.match(line) or _CHAPTER.match(line):
            break
        cells = _cells(line)
        if current and len(cells) >= 5 and cells[0].isdigit() and cells[1]:
            packages[current]['devices'].append({'name': cells[1], 'normalised': _normalise(cells[1]),
                                                 'budget_unit_text': cells[4], 'line': number})
            misses = 0
            continue
        misses += 1
        if misses >= 3:
            break
    return packages, heading


def _section_clauses(lines, heading_number, title, tables_by_heading):
    """一个设备标题下的要求行，以及它覆盖到哪一行。

    有 docx 解析报告时，「标题 + 下面那张表」是直接读出来的（表区间由报告
    给出，查表用的是**完整标题行**，序号是它的一部分）；没有报告时退化成
    「标题后连续的要求行」，并在遇到下一个标题、小标题、空行或章节头时停下。
    """
    # 同一个标题会在多个包里各出现一次（「一、血型血清学离心机」既可能在 1 包
    # 也可能在 4 包）：要取标题行正好在这一处的那张表。取第一张会让第二次遇到
    # 同名标题时行号倒退，解析不前进。
    table = next((item for item in tables_by_heading.get(title) or []
                  if item.get('heading_line') == heading_number), None)
    if table is not None:
        entries = [_clause(lines[number], number)
                   for number in range(table['start_line'], table['end_line'] + 1)]
        return [entry for entry in entries if entry], max(table['end_line'] + 1, heading_number + 1), table
    entries, number = [], heading_number + 1
    while number < len(lines):
        line = lines[number]
        if not line.strip() or _LIST_ITEM_PREFIX.match(line) or _SECTION_HEADING.match(line) or _CHAPTER.match(line):
            break
        if _DEVICE_HEADING.match(line) and not _CLAUSE.match(line):
            break
        entry = _clause(line, number)
        if entry:
            entries.append(entry)
        elif entries:
            entries[-1]['text'] += line.strip()
        else:
            break
        number += 1
    return entries, number, None


def requirement_sections(lines, tables=None):
    """包 → [{标题, 要求行}]。绑定链的第 2 步（品类 → 要求）。"""
    tables_by_heading = {}
    for table in tables or []:
        tables_by_heading.setdefault(table.get('heading'), []).append(table)
    sections, current, number = {}, None, 0
    while number < len(lines):
        line = lines[number]
        section = _SECTION_HEADING.match(line)
        if section:
            current = section.group(1) + '包'
            sections.setdefault(current, [])
            number += 1
            continue
        if _CHAPTER.match(line):
            current = None
        if current:
            heading = _DEVICE_HEADING.match(line)
            # 正文小标题（「二、商务要求」）后面没有参数表，不会被当成设备：
            # 要么报告里有一张以它为标题的表，要么它后面跟着要求行。
            if heading and not _CLAUSE.match(line):
                # 查表要用完整标题行（含序号），配对用去掉序号的品类名。
                title, name = line.strip(), _normalise(heading.group('name'))
                heading_number = number
                entries, number, table = _section_clauses(lines, number, title, tables_by_heading)
                if entries:
                    sections[current].append({'title': title, 'normalised': name,
                                              'requirements': entries,
                                              'heading_line': heading_number,
                                              'table_heading_line': (table or {}).get('heading_line'),
                                              'line_start': (table or {}).get('start_line'),
                                              'line_end': (table or {}).get('end_line')})
                    continue
        number += 1
    return sections


def award_index(sources):
    """中标公告 → ``(包, 设备名)`` → 品牌/型号/单价。绑定链的第 3 步。"""
    index = {}
    for source in sources:
        text = source.get('body_text') or ''
        if source.get('body_status') != 'fetched' or '标段名称' not in text or '规格型号' not in text:
            continue
        package, current = None, None
        for number, line in enumerate(text.split('\n')):
            matched = _AWARD_PACKAGE.search(line)
            if matched:
                package = matched.group(1) + '包'
                continue
            field = _AWARD_FIELD.match(line)
            if not field or not package:
                continue
            name, value = field.group(1), field.group(2)
            if name == '名称':
                current = {'package': package, 'name': value, 'line': number,
                           'notice_url': source.get('url'), 'notice_title': source.get('title')}
                index[(package, _normalise(value))] = current
            elif current is not None:
                key = {'品牌': 'brand', '规格型号': 'model', '数量': 'quantity'}.get(name)
                if key is None and name.startswith('单价'):
                    key = 'unit_price'
                if key:
                    current[key] = value
    return index


def tender_document(sources):
    """挑出这份运行里真正的招标文件（发布方自己发的那一份）。"""
    candidates = []
    for source in sources:
        text = source.get('body_text') or ''
        if source.get('body_status') != 'fetched' or '技术参数要求' not in text or '采购需求' not in text:
            continue
        candidates.append(source)
    if not candidates:
        return None
    # 附件（带 parent_url 的那一份）才是发布方的招标文件；同一条正文被多处
    # 收录时取最有内容的一份。
    candidates.sort(key=lambda source: (not source.get('parent_url'), -len(source.get('body_text') or '')))
    return candidates[0]


def _line_numbers_intact(lines, tables):
    """正文行与解析报告是否同源：不同源时宁可退化成行序推断，也不串行绑定。"""
    for table in tables:
        heading, number = table.get('heading'), table.get('heading_line')
        if heading and isinstance(number, int) and 0 <= number < len(lines) and lines[number].strip() == heading:
            return True
    return False


def _statement(device_name, text):
    return f'本项目要求：{device_name} {text} 中标人须满足。这不等于该型号的产品标称参数。'


def _requirement_row(entry, device_name, package):
    return {**entry, 'statement': _statement(device_name, entry['text']), 'package': package}


def _binding_chain(document, package, device, section, award, list_heading=None):
    chain = [{'step': '采购清单', 'document': document.get('url'), 'line': device.get('line'),
              'heading': list_heading,
              'evidence': f'「{list_heading or "采购设备清单"}」第 {device.get("line")} 行列出的 '
                          f'{package} 设备「{device.get("name")}」'}]
    span = [section.get('line_start'), section.get('line_end')]
    chain.append({'step': '技术参数要求', 'document': document.get('url'),
                  'lines': span if None not in span else [section.get('heading_line')],
                  'heading': section.get('title'),
                  'evidence': f'「{section.get("title")}」标题下 {len(section["requirements"])} 条要求，'
                              f'其中 {sum(1 for r in section["requirements"] if r["key"])} 条为 ▲ 关键条款'})
    if award:
        chain.append({'step': '中标公告', 'url': award.get('notice_url'), 'line': award.get('line'),
                      'evidence': f'{package} 中标「{award.get("name")}」 {award.get("brand", "")} '
                                  f'{award.get("model", "")}'.strip()})
    else:
        chain.append({'step': '中标公告', 'url': None, 'line': None,
                      'evidence': '这次运行里没有把该包的中标型号与本品类对应起来，未建立最后一步'})
    return chain


def record(run, sources=None):
    """抽取并写入 ``run['tender_requirements']``；没有可抽的内容时返回 False。"""
    sources = list(sources if sources is not None else run.get('sources') or [])
    document = tender_document(sources)
    if document is None:
        return False
    lines = (document.get('body_text') or '').split('\n')
    report = document.get('parse_report') or {}
    tables = report.get('table_index') if isinstance(report.get('table_index'), list) else []
    if tables and not _line_numbers_intact(lines, tables):
        tables = []
    sections = requirement_sections(lines, tables)
    if not sections:
        return False
    packages, list_heading = device_list(lines, tables)
    awards = award_index(sources)

    # 同一品类出现在多个包里就不绑定：那只能是项目级要求。
    occurrences = {}
    for package, items in sections.items():
        for item in items:
            occurrences.setdefault(item['normalised'], set()).add(package)

    result = {'version': 1, 'evidence_type': EVIDENCE_TYPE, 'evidence_level': EVIDENCE_LEVEL,
              'nature': NATURE,
              'documents': [{'url': document.get('url'), 'title': document.get('title'),
                             'parent_url': document.get('parent_url'),
                             'chapter': '第四章 采购需求',
                             'table_rows': report.get('table_rows')}],
              'packages': [], 'project_level': []}
    for package in sorted(sections, key=_package_order):
        listed = {device['normalised']: device for device in (packages.get(package) or {}).get('devices', [])}
        budget = packages.get(package) or {}
        entry = {'package': package,
                 'budget': {'text': budget.get('budget_text'), 'line': budget.get('budget_line')},
                 'devices': []}
        for section in sections[package]:
            name = section['normalised']
            rows = [_requirement_row(row, name, package) for row in section['requirements']]
            if len(occurrences.get(name, ())) > 1:
                result['project_level'].append({
                    'name': name, 'title': section['title'], 'package': package,
                    'reason': '同一品类出现在多个包，无法绑定到单一包，只作为项目级要求展示',
                    'requirements': rows})
                continue
            device = listed.get(name) or {'name': section['title'], 'line': None}
            award = awards.get((package, name))
            entry['devices'].append({
                'name': name, 'title': section['title'],
                'award': award,
                'requirements': rows,
                'requirement_source': {'url': document.get('url'),
                                       'line_start': section.get('line_start'),
                                       'line_end': section.get('line_end'),
                                       'heading': section.get('title'),
                                       'heading_line': section.get('table_heading_line')},
                'binding_chain': _binding_chain(document, package, device, section, award, list_heading)})
        if entry['devices']:
            result['packages'].append(entry)
    if not result['packages'] and not result['project_level']:
        return False
    result['notes'] = [
        '本项目的采购要求按包、按设备组织；▲ 为招标文件里的关键条款。',
        '中标人须满足这些要求，但这不等于该型号的产品标称参数。']
    run['tender_requirements'] = result
    return True
