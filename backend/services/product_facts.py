"""Deterministic, located manufacturer-table facts for the selected model."""
import re
from backend.services.procurement_rules import contains_model, identity

#: 资料分析的字段名 → 报告里用的中文名。
#:
#: 与前端「型号与参数」页是同一套说法（``frontend/src/pages/TaskWorkspace.jsx`` 的
#: ``fieldLabels``）。报告里印 ``product_identity_and_configuration`` 这种字段名，
#: 对采购人员就是内部话；两处各写一份又会各自漂移，所以
#: ``tests/test_report_frontend_consistency.py`` 逐条比对这两个表。
FIELD_LABELS = {
    'product_identity_and_configuration': '产品型号与配置',
    'core_function_and_applications': '用途与主要功能',
    'detection_or_processing_modes': '检测与处理模式',
    'capacity_and_throughput': '容量与处理量',
    'sample_and_format_compatibility': '样品与容器兼容性',
    'performance_range': '性能与检测范围',
    'accuracy_sensitivity_and_repeatability': '精度、灵敏度与重复性',
    'temperature_environment_and_safety': '温度、环境与安全',
    'software_data_and_compliance': '软件、数据与合规',
    'accessories_consumables_and_options': '附件与耗材',
    'physical_dimensions_power_and_installation': '尺寸、电源与安装',
    'price_warranty_delivery_and_service': '价格、质保与售后',
}


def field_label(name):
    """字段名的中文说法；认不出的字段直接不展示（不印内部字段名）。"""
    return FIELD_LABELS.get(str(name or '').strip())


def _field_value_text(value):
    """字段值写成一行人话：``键：值``；值本身是列表就顿号连起来。"""
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if isinstance(item, list):
                item = '、'.join(str(entry) for entry in item if str(entry).strip())
            text = str(item).strip()
            if text:
                parts.append(f'{key}：{text}')
        return '；'.join(parts)
    if isinstance(value, list):
        return '、'.join(str(item) for item in value if str(item).strip())
    return str(value or '').strip()


def _field_evidence(field, documents=None):
    """字段的原文出处：同一文档同一页合并成一个入口（与前端 ``mergeEvidence`` 同口径）。"""
    entries = {}
    for source in field.get('sources') or []:
        if not isinstance(source, dict):
            continue
        document_id = str(source.get('document_id') or '').strip()
        location = str(source.get('page_or_location') or '').strip()
        match = re.search(r'page:(\d+)', location)
        page = int(match.group(1)) if match else None
        key = (document_id, page)
        entry = entries.get(key)
        if entry is None:
            entry = {'document_id': document_id, 'location': location, 'excerpt': str(source.get('excerpt') or '').strip()}
            if page:
                entry['page'] = page
            if documents and document_id in documents:
                entry['document_name'] = documents[document_id]
            entries[key] = entry
        elif not entry.get('excerpt') and source.get('excerpt'):
            entry['excerpt'] = str(source['excerpt']).strip()
    return list(entries.values())


def document_facts(candidates, brand, model, documents=None):
    """上传资料里资料分析已经提取、且带页码的字段。

    这一整组字段一直在页面上（「型号与参数」页完整展示，还带「查看原始资料 · 第 N 页」
    入口），报告却只读联网来源的厂家表格（:func:`table_facts`），于是同一份资料在页面上
    有 11 组、在报告里写着「尚未取得本型号完整厂家参数」——不是编造，是手上的证据没用上。

    只认型号能对上（``identity`` 相等）的那一份候选：资料里有多台机器时不猜。
    出处写清是**上传资料**（``document_id`` + 页码），与公开来源分开；被用户排除（``rejected``）
    或资料未提供（``unknown``／空值）的字段不进来。
    """
    target = identity(model)
    if not target:
        return []
    for candidate in candidates or []:
        if not isinstance(candidate, dict) or identity(candidate.get('model')) != target:
            continue
        items = []
        for field in candidate.get('fields') or []:
            if not isinstance(field, dict) or field.get('status') in {'rejected', 'unknown'}:
                continue
            label = field_label(field.get('field_name'))
            text = _field_value_text(field.get('value'))
            if not label or not text or text.lower() in {'unknown', 'none', 'null'}:
                continue
            if field.get('evidence_status') == 'unparsed':
                text = text + '；（资料原文里有这段内容，但未结构化成单条参数，仅供人工核对）'
            item = {'label': '上传资料 · ' + label, 'text': text}
            evidence = _field_evidence(field, documents)
            if evidence:
                item['evidence'] = evidence
            items.append(item)
        return items
    return []


def table_facts(sources, model):
    facts = []
    for source in sources:
        if source.get('body_status') != 'fetched' or source.get('source_type') not in {'manufacturer_website', 'manufacturer_download'}:
            continue
        for line in source.get('body_text', '').splitlines():
            if not line.startswith('[表格按列展开') or not contains_model(line, model):
                continue
            location, text = line.split(']', 1)
            facts.append({'model': model, 'text': text.strip(), 'status': 'manufacturer_stated',
                          'evidence': {'url': source['url'], 'excerpt': line,
                                       'location': location.lstrip('['),
                                       'char_start': source['body_text'].find(line)}})
    return facts


def _as_evidence_list(evidence):
    """一条依据的固定形状：列表（``document_facts`` 给的是列表，``table_facts`` 给的是单条）。

    原来直接把它塞进 ``[evidence]``，遇到列表就成了嵌套列表，导出时整条 dict 被
    印成 ``来源：[{'document_id': …}]``（B01 回放第 39/41 行）。
    """
    if isinstance(evidence, list):
        return list(evidence)
    return [evidence] if evidence else []


def requirement_notes(facts, parameters, document_facts=()):
    """本轮每条实验要求：资料里查到对应写法就连出处一起给出，没查到就如实说没有。

    这一节叫「产品事实与用途适配」，所以条目必须说清自己是哪一类：查得到的写
    「上传资料 · …」并带出处，查不到的**不再以产品事实的口吻陈述一个数值**——
    标成「本轮要求 · …」，并写明这个数来自比较基准、本轮资料与公开来源都没有
    给出该型号可直接核对的对应值。原来那三条只有一句「需求已记录：100.0」，
    evidence 全空，读起来却像是产品参数。
    """
    rows = list(facts) + list(document_facts)
    notes = []
    for requirement in parameters:
        name = requirement['name']
        matching = [f for f in rows if any(label in f['text'] for label in (name, '使用温度范围' if '温度' in name else name))]
        value = str(requirement['value']) + (f" {requirement['unit']}" if requirement.get('unit') else '')
        label = '本轮要求 · ' + name
        if requirement.get('direction') == 'covers' and '温度' in name:
            for fact in matching:
                match = re.search(r'(-?\d+(?:\.\d+)?)\s*[~～–—至-]\s*(-?\d+(?:\.\d+)?)', fact['text'])
                if match:
                    lower, upper = map(float, match.groups())
                    low, high = requirement['value']
                    notes.append({'label': label, 'text': '厂家温区覆盖本次使用范围；温度稳定性须按实际温度和承诺值另行验收。' if lower <= low <= high <= upper else '厂家温区未覆盖本次完整使用范围。', 'evidence': _as_evidence_list(fact['evidence'])})
                    break
            else:
                notes.append({'label': label, 'text': '尚无归属于本型号的完整温区证据。'})
        elif matching:
            notes.append({'label': label,
                          'text': f'比较基准记录的本轮要求是 {value}；本轮取得的资料里对应写法：{matching[0]["text"]}。标称值不等于验收值。',
                          'evidence': _as_evidence_list(matching[0]['evidence'])})
        else:
            notes.append({'label': label,
                          'text': f'比较基准记录的本轮要求是 {value}；本轮上传资料与公开来源都没有给出该型号可直接核对的对应值（未说明不等于不具备）。'})
    return notes


def dimension_conflicts(sources, model):
    """Keep conflicting dimensions from model-owned rows visible with both sources."""
    observations = []
    for source in sources:
        if source.get('body_status') != 'fetched' or source.get('source_type') not in {'manufacturer_website', 'manufacturer_download', 'tender_document'}:
            continue
        for line in source.get('body_text', '').splitlines():
            if not contains_model(line, model):
                continue
            for match in re.finditer(r'(内尺寸|内部尺寸|工作室尺寸)\s*(?:[（(][^）)]{0,30}[）)])?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(mm|毫米)?', line):
                # Without a stated unit in the row/header, values cannot compare.
                if not match[5] and not re.search(r'(?:内尺寸|内部尺寸|工作室尺寸)[^）)]{0,30}mm', line, re.I):
                    continue
                observations.append({'value': tuple(float(match[i]) for i in (2,3,4)),
                    'text': match[0], 'evidence': {'url': source['url'], 'excerpt': line},
                    'type': source['source_type']})
    if len({o['value'] for o in observations}) < 2:
        return []
    return [{'label': '同型号尺寸存在差异',
             'text': '不同来源记载：' + '；'.join(dict.fromkeys(o['text'] for o in observations)) + '。不能视为相同配置；需厂家澄清版本、尺寸顺序及交付实物。历史公告不能覆盖当前厂家参数。',
             'evidence': list({o['evidence']['url']: o['evidence'] for o in observations}.values())}]


#: 型号后缀最多认几个字母。``XQ-20`` 与 ``XQ-20iu``、``HK-3`` 与 ``HK-3TM`` 这类
#: "同一台机器的两种写法（后缀对应不同配置版本）"要能被认出来；再长就不是后缀了。
MODEL_SUFFIX_LIMIT = 4

#: 每个来源最多扫多少字符的正文。真实一轮的 ``body_text`` 合起来可能十几 MB，报告是
#: 读的时候现算的，得有个上限，超出的部分不参与"写法统计"（不影响任何判据）。
MODEL_SCAN_LIMIT = 200_000


def _clip(text, limit):
    """把一句话截到 ``limit`` 字，用来给读者看"上下文"。"""
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def model_spelling_pattern(model):
    """把比较基准记录的型号放宽成「同一台机器的各种写法」。

    中间允许空格/连字符/下划线（``XQ-20`` / ``XQ 20`` / ``XQ20`` 都算），后面允许最多
    ``MODEL_SUFFIX_LIMIT`` 个字母（配置版本后缀）。**这只用于"把原文里的写法列出来"**：
    判据（``contains_model`` / ``_target_pattern``）一个字都没有放宽或
    收紧，报告也不会因为认出后缀就把带后缀的那台当成别的机器。
    """
    core = re.sub(r'[\s\-_]+', '', str(model or ''))
    if len(core) < 3:
        return None
    body = r'[\s\-_]*'.join(re.escape(char) for char in core)
    return re.compile(body + r'(?P<suffix>[A-Za-z]{0,%d})' % MODEL_SUFFIX_LIMIT, re.I)


def model_form_ambiguity(sources, documents, model):
    """原文里同一台机器出现了几种写法？各几次？上下文差在哪？

    盲测发现：某份资料里同一台仪器带后缀出现 9 次、不带后缀出现 15 次，后缀对应的
    是"带孵育"的那个配置版本，而报告只写了其中一种、也没说原文有两种写法——读者据此
    判断会买错机器。产品不替读者选：这里把两种写法、次数和上下文并列出来，并写明报告
    正文用的是哪一种口径，剩下的交给读者去确认。
    """
    pattern = model_spelling_pattern(model)
    if not pattern:
        return None
    forms = {}

    def note(text, url):
        form = re.sub(r'\s+', ' ', text).strip()
        key = form.lower()
        entry = forms.setdefault(key, {'form': form, 'count': 0, 'sample': '', 'url': url})
        entry['count'] += 1
        if not entry['sample'] and url:
            entry['url'] = url
        return entry

    for source in sources or []:
        body = str(source.get('body_text') or '')[:MODEL_SCAN_LIMIT]
        if not body:
            continue
        for line in body.splitlines():
            if not pattern.search(line):
                continue
            for match in pattern.finditer(line):
                entry = note(match.group(0), source.get('url'))
                if not entry['sample']:
                    entry['sample'] = _clip(line.strip(), 90)
    for candidate in documents or []:
        for field in candidate.get('fields') or []:
            for source in field.get('sources') or []:
                excerpt = str(source.get('excerpt') or '')
                if not pattern.search(excerpt):
                    continue
                for match in pattern.finditer(excerpt):
                    entry = note(match.group(0), source.get('url') or source.get('document_id'))
                    if not entry['sample']:
                        entry['sample'] = _clip(excerpt.strip(), 90)
    if len(forms) < 2:
        return None
    ordered = sorted(forms.values(), key=lambda item: (-item['count'], item['form']))
    listed = '、'.join('%s（%d 处）' % (item['form'], item['count']) for item in ordered)
    contexts = '；'.join('%s 的上下文：「%s」' % (item['form'], item['sample'])
                        for item in ordered if item['sample'])
    evidence = [{'url': item['url'], 'excerpt': item['sample']}
                for item in ordered if item.get('url')]
    return {'label': '型号写法有歧义',
            'text': ('本轮来源里这个型号出现过 %d 种写法：%s。%s报告正文按比较基准记录的写法「%s」处理；'
                     '这几种写法是不是同一台机器、后缀对应哪个配置版本，需要向销售或厂家确认后才能定。'
                     % (len(ordered), listed, (contexts + '。') if contexts else '', model)),
            'evidence': evidence}
