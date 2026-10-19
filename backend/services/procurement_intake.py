"""Persistent, evidence-bound intake. User statements are drafts, not product facts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

FIELDS = {
    'category': '仪器品类', 'brand': '品牌', 'model': '完整型号',
    'scenario': '实验用途', 'requirements': '必须满足的实验要求',
    'supplier_price': '销售单价', 'quantity': '采购数量', 'currency': '币种',
    'configuration': '报价包含的配置', 'tax': '税费', 'shipping': '运费',
    'installation': '安装培训', 'warranty': '保修期限',
    'service_response': '报修响应时间', 'service_onsite': '上门处理时间',
    'delivery': '交货时间', 'recording': '数据记录与导出要求',
}
GROUPS = [
    ('product', ['category', 'brand', 'model'], '销售推荐的仪器品类、品牌和完整型号分别是什么？可以直接粘贴报价中的一行。'),
    ('use', ['scenario', 'requirements'], '主要做什么实验？常用工作范围或最不能妥协的指标是什么？用日常语言描述即可。'),
    ('recording', ['recording'], '你提到需要记录。是自动连续记录并导出 Excel/CSV 或打印留档，还是只查看仪器上的历史数据？'),
    ('quote', ['supplier_price', 'quantity', 'currency'], '销售报的单价、币种和采购数量是多少？如给的是总价，请明确说明。'),
    ('configuration', ['configuration', 'tax', 'shipping', 'installation'], '这份报价包含哪些配件或软件？是否含税、运费和安装培训？销售没说明的可以直接说不知道。'),
    ('service', ['warranty', 'service_response', 'service_onsite'], '销售承诺保修多久？报修后多久响应、多久能上门？这三项分别说明；未承诺的可以留空。'),
    ('delivery', ['delivery'], '销售承诺多久交货？交货时间与保修、报修响应时间会分别记录。'),
]


def requirement_quote_allowed(message, quote):
    """Supplier capabilities cannot silently replace buyer requirements."""
    supplier_reply = re.search(r'(?:销售|供应商).{0,8}(?:回复|表示|说|承诺)|我是销售', message)
    if not supplier_reply:
        return True
    if not isinstance(quote, str) or quote not in message:
        return False
    end = message.index(quote) + len(quote)
    sentence = re.split(r'[。！？!?\n]', message[:end])[-1]
    buyer = list(re.finditer(r'(?:我|我们|实验|采购).{0,4}(?:需要|要求)|需求(?:改为|仍是|不变)', sentence))
    seller = list(re.finditer(r'(?:销售|供应商).{0,8}(?:回复|表示|说|承诺)', sentence))
    return bool(buyer and (not seller or buyer[-1].start() > seller[-1].start()))


def next_question(state):
    facts, deferred = state.get('facts', {}), state.get('deferred', [])
    text = ' '.join(str(v.get('value', '')) for v in facts.values())
    for group, keys, question in GROUPS:
        if group in deferred:
            continue
        if group == 'recording' and not any(w in text for w in ('记录', '导出', '留档', '打印')):
            continue
        missing = [key for key in keys if not facts.get(key, {}).get('value')]
        if group == 'recording' and not any(w in str(facts.get('recording', {}).get('value', '')) for w in ('导出', '打印', '历史', 'CSV', 'Excel', '不需要')):
            missing = ['recording']
        if missing:
            if len(missing) != len(keys):
                question = '还缺少' + '、'.join(FIELDS[k] for k in missing) + '。请补充；销售未说明的可以说不知道。'
            return {'group': group, 'text': question}
    return {'group': 'review', 'text': '已整理你提供的条件。请核对下方比较基准；缺少的销售承诺会在报告中保留，不会默认已包含。也可以继续补充或纠正。'}


def read_intake(repo, task_id):
    saved = repo.get('procurement_records', 'intake-' + task_id)
    if saved:
        return with_document_suggestions(repo, task_id, saved)
    task = repo.get('tasks', task_id) or {}
    return with_document_suggestions(repo, task_id, {'id': 'intake-' + task_id, 'kind': 'intake', 'task_id': task_id,
            'revision': 0, 'facts': {}, 'parameters': [], 'deferred': [],
            'messages': [{'role': 'user', 'content': task.get('scenario', '')}],
            'question': {'group': 'use', 'text': '已收到产品信息。它主要做什么实验？常用工作范围和必须具备的功能是什么？有报价也可以一起补充。'}})


#: 对话里那条「资料型号与口述型号」说明的标记。``with_document_suggestions`` 每次
#: 读取都重算它，靠这个标记把上一次写下的那条摘掉再加，所以重复读不会越堆越多。
DOCUMENT_NOTICE = 'document_identity'


def model_identity(value):
    """型号比对用的写法归一：大小写、空格、连字符、下划线的差异不算不同型号。"""
    return re.sub(r'[\s\-_/]+', '', str(value or '')).upper()


def spoken_fact(state, key):
    """用户自己在本任务里说过的字段值；``origin`` 不是 ``user_statement`` 的一律不算口述。

    资料分析出的型号只进 ``draft``、从不写进 ``facts``；这里再按 ``origin`` 挡一道，
    免得将来有别的来源写进 ``facts`` 时，拿资料型号去「印证」它自己。
    ``origin`` 缺失的老记录按用户陈述处理。
    """
    entry = (state.get('facts') or {}).get(key) or {}
    if str(entry.get('origin') or 'user_statement') != 'user_statement':
        return ''
    return str(entry.get('value') or '').strip()


def spoken_model_keys(model, brand=''):
    """用户口述型号的等价写法集合，用来判断资料里的某一台是不是他点名的那台。

    口述可能写成 ``1300A2``、``知楚1300A2`` 或 ``知楚 1300A2``：先按 ``model_identity``
    归一化，再在本任务已记下的品牌确实构成前缀时剥掉品牌。集合里放的是**等价写法**，
    不是相似度分数——匹配永远是布尔的，不做「谁更像」的排序。
    """
    key = model_identity(model)
    if not key:
        return set()
    keys = {key}
    prefix = model_identity(brand)
    if prefix and key.startswith(prefix) and len(key) > len(prefix):
        keys.add(key[len(prefix):])
    return keys


def _matches_spoken(candidate_model, spoken_keys):
    """资料里的这个型号是不是用户口述的那一台。"""
    key = model_identity(candidate_model)
    if not key or not spoken_keys:
        return False
    if key in spoken_keys:
        return True
    # 口述写成 ``知楚1300A2``、资料只写 ``1300A2``（或品牌没记进 facts）时，按**后缀**
    # 认定同一台。只认后缀，并要求资料型号 >= 4 位且含数字：``A2``、``130`` 这类通用
    # 片段够不着任何一台，``1300A2`` 也不会认成以 ``1500A2`` 结尾的那一台。
    if len(key) < 4 or not re.search(r'\d', key):
        return False
    return any(spoken.endswith(key) for spoken in spoken_keys)


def document_identity(repo, task_id, spoken_model='', spoken_brand=''):
    """最新一次资料分析里识别出的产品型号。

    只有在**唯一可用**时才给出结论：候选之间无法取舍时不猜，交给用户选，
    免得把 A 机器的资料当成 B 机器的比较基准。

    资料里出现多个可用候选时，先用**用户在本任务里自己说过的型号**
    （``spoken_model``，只来自 ``origin=user_statement``）收敛一次：恰好命中一台才按
    ``unique`` 处理，命中多台或一台都没命中仍然交回用户选。``conflicted``
    （原文互相矛盾、还没结论）一律不收敛。
    """
    analyses = [r for r in repo.list('reports', task_id) if r.get('kind') == 'deepseek_analysis']
    report = max(analyses, key=lambda r: r.get('created_at', ''), default={})
    candidates = [c for c in (report.get('candidates') or [])
                  if isinstance(c, dict) and str(c.get('model') or '').strip()]
    usable = [c for c in candidates if c.get('sources') and c.get('status') not in {'rejected', 'conflicted'}]
    conflicted = [c for c in candidates if c.get('status') == 'conflicted']
    # ``rejected`` 是用户自己排除过的产品，不算冲突，也不该每次都再问一遍；
    # ``conflicted`` 是原文互相矛盾、还没有结论，必须问。
    listed = [{'brand': c.get('brand'), 'model': str(c['model'])}
              for c in candidates if c.get('status') != 'rejected']
    if conflicted:
        return {'status': 'ambiguous', 'options': listed}
    if len(usable) > 1:
        matched = [c for c in usable
                   if _matches_spoken(c.get('model'), spoken_model_keys(spoken_model, spoken_brand))]
        if len(matched) == 1:
            # 收敛成「资料里就是这一台」：对话里给结论，不再把全部候选摆出来让用户挑。
            # ``converged_total`` 只用于说明资料里另有几台，不参与取舍。
            return {'status': 'unique', 'converged': True, 'converged_total': len(listed),
                    'identity': {k: matched[0][k] for k in ('brand', 'model', 'category') if matched[0].get(k)}}
        return {'status': 'ambiguous', 'options': listed}
    if len(usable) == 1:
        return {'status': 'unique',
                'identity': {k: usable[0][k] for k in ('brand', 'model', 'category') if usable[0].get(k)}}
    return {'status': 'none'}


def _named(brand, model):
    return ' '.join(part for part in (str(brand or '').strip(), str(model or '').strip()) if part)


def _amount_text(value):
    try:
        number = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f'{number:g}'


def _identified_notice(identity):
    return (f"产品资料里识别到的型号是 {_named(identity.get('brand'), identity.get('model'))}，"
            '已按产品资料为准预填为比较基准。资料上的参数是厂家标称值，不等于你的实验要求；'
            '配置和商务条件仍按待核实处理。')


def _override_notice(identity, spoken, draft):
    """说清三件事：基准是哪台、为什么换了、原来那笔报价还算不算数。"""
    document_name = _named(identity.get('brand'), identity.get('model'))
    spoken_name = _named(spoken['brand'], spoken['model'])
    price = draft.get('supplier_price')
    quote = (f"销售提到的 {spoken_name} 和 {_amount_text(price)} 元报价" if price not in (None, '')
             else f'销售提到的 {spoken_name}')
    return (f'产品资料里识别到的型号是 {document_name}，与销售提到的 {spoken_name} 不是同一台。'
            f'按“有产品资料就以资料里的型号为准”，已把比较基准改为 {identity["model"]}。'
            f'{quote}仍保留为待核实的销售推荐：这笔报价只对应 {spoken["model"]}，'
            f'不能当成 {identity["model"]} 的报价；要判断 {identity["model"]} 的价格，'
            '需要销售按这个型号重新报价。')


def _ambiguous_notice(options):
    listed = '、'.join(_named(option.get('brand'), option.get('model')) for option in options)
    return (f'这次资料分析识别出多个候选型号（{listed}），无法确定哪一台是你要的，'
            '比较基准保持原样、没有替换。请回复要用哪个型号，'
            '或先在“分析”页把不对的产品排除掉。')


def _converged_notice(identity, others):
    """资料里有多台、用户自己点过名时说清基准是哪台；别的候选没被静默丢掉。"""
    tail = f'资料里另有 {others} 个型号，已按你提到的型号收敛到这一台。' if others > 0 else ''
    return (f"产品资料里识别到的型号是 {_named(identity.get('brand'), identity.get('model'))}，"
            '就是你提到的型号，已按产品资料为准预填为比较基准。'
            f'{tail}资料上的参数是厂家标称值，不等于你的实验要求；'
            '配置和商务条件仍按待核实处理。')


def with_document_suggestions(repo, task_id, state):
    """产品资料的型号优先作为比较基准；口述的型号和它的报价另记，不会被顶掉。

    用户没上传资料、或资料识别不出唯一型号时，比较基准仍是口述型号。
    资料里有多台而用户自己点过名时，先按口述型号收敛；收敛不了才把候选列出来让他选。
    口述型号始终留在 ``facts`` 里（``origin=user_statement``），资料型号只进 ``draft``，
    不会变成用户陈述；两者不同时，报价记到 ``quote_model`` 上，免得把口述型号的报价
    安到资料型号头上。
    """
    state = dict(state)
    state['messages'] = [m for m in (state.get('messages') or []) if m.get('kind') != DOCUMENT_NOTICE]
    for key in ('document_suggestions', 'document_identity', 'document_candidates'):
        state.pop(key, None)
    spoken = {key: spoken_fact(state, key) for key in ('brand', 'model')}
    found = document_identity(repo, task_id, spoken['model'], spoken['brand'])
    if found['status'] == 'none':
        return state
    if found['status'] == 'ambiguous':
        state['document_candidates'] = found['options']
        state['messages'] = [*state['messages'], {'role': 'assistant', 'kind': DOCUMENT_NOTICE,
                                                  'content': _ambiguous_notice(found['options'])}]
        return state
    identity = dict(found['identity'])
    facts = state.get('facts') or {}
    # 收敛（资料多台、用户点名的正好一台）本身就是「同一台」的证据，不能再按字面判成
    # 两台机器去搬报价归属；其余情况下这条判定与原来完全一致。
    same_machine = (model_identity(spoken['model']) == model_identity(identity.get('model'))
                    or bool(found.get('converged')))
    overrides = bool(spoken['model']) and not same_machine
    state['document_suggestions'] = identity
    state['document_identity'] = {**identity, 'spoken_brand': spoken['brand'], 'spoken_model': spoken['model'],
                                  'overrides_spoken': overrides, 'converged': bool(found.get('converged'))}
    draft = {**draft_baseline(state), **identity}
    if overrides:
        draft['quote_model'] = spoken['model']
        if spoken['brand']:
            draft['quote_brand'] = spoken['brand']
    state['draft'] = draft
    temporary_facts = {**facts, **{key: {'value': value} for key, value in identity.items()}}
    state['question'] = next_question({**state, 'facts': temporary_facts})
    if found.get('converged'):
        notice = _converged_notice(identity, int(found.get('converged_total') or 1) - 1)
    else:
        notice = _override_notice(identity, spoken, draft) if overrides else _identified_notice(identity)
    state['messages'] = [*state['messages'], {'role': 'assistant', 'kind': DOCUMENT_NOTICE,
                                              'content': notice}]
    return state


def draft_baseline(state):
    values = {k: v['value'] for k, v in state['facts'].items()}
    for key in ('supplier_price', 'quantity'):
        try:
            value = float(str(values.get(key, '')).replace(',', ''))
            values[key] = value if value >= 0 and (key != 'quantity' or value > 0) else None
        except (ValueError, TypeError):
            values[key] = None
    requirements = [values.pop(k, '') for k in ('requirements', 'recording')]
    parts = []
    for item in [values.get('scenario', ''), *requirements]:
        text = str(item).strip()
        if text and not any(text in existing for existing in parts):
            parts = [existing for existing in parts if existing not in text]
            parts.append(text)
    values['scenario'] = '；'.join(parts)
    values['parameters'] = state.get('parameters', [])
    values['intake_revision'] = state['revision']
    return values


def respond(repo, task_id, payload, client_factory):
    state = read_intake(repo, task_id)
    if payload.get('initialize') and state['revision']:
        return state
    if payload.get('revision') != state['revision']:
        raise ValueError('这段对话已有新回复，请刷新后继续，避免覆盖已保存的条件。')
    message = str(payload.get('message') or '').strip()
    if not message or len(message) > 6000:
        raise ValueError('请填写本次补充信息，最多6000字。')
    # Only current user text and the original first message may justify changes.
    initial = state['messages'][0]['content'] if not state['revision'] else ''
    evidence_text = initial + '\n' + message
    prompt = '''你是实验室采购需求访谈员。只从本次用户原文提取事实，已有条件仅用于理解指代。
产品资料、模型常识不是用户已确认事实。不得猜测配置、币种、税费、保修、售后时间、记录能力。
返回JSON {"facts":{"字段":{"value":"值","quote":"本次原文连续引文"}},
"parameters":[{"name":"指标","value":"值或数字或两端数字数组","unit":"单位","direction":"equal|min|max|covers","conditions":"适用条件","quote":"本次原文连续引文"}]}。
只输出有原文支持的新增或更正字段。用户说不知道的字段不要赋值。
supplier_price只记录明确单价，单位为元；有总价及明确数量才可以换算。quantity为数字，currency只在明确人民币/元时写CNY。
requirements写用户要求原意；recording仅在明确记录方式时提取，不把“有记录”变成连续导出。
parameters是用户必须满足的要求，不是当前仪器能力；例如25-42度写covers [25,42] ℃。
销售回复中的设备能力写configuration，不写parameters、requirements、recording或scenario。只有采购方明确提出或更正要求时才更新要求；不得将销售“可以导出CSV”替换原有连续记录要求。
conditions只写影响可比性的测量条件，例如空载、测量温度；不要把“用于微生物培养”“用于留档”等用途放进conditions。
“温度稳定”没有明确数值时不能发明±0.5℃等数值，应保留文字要求。
需要连续记录并导出或打印必须作为一项指标保留；不要把有接口当作能记录导出。
更正某指标时用相同name；没有明确更正不要删除原要求。
保修期限、报修响应、上门处理、交货时间是不同字段，不得互相填充。
可提取字段：''' + json.dumps(FIELDS, ensure_ascii=False)
    unknown_answer = message in {'不知道', '不清楚', '暂不清楚', '销售没说', '销售未说明', '暂未说明'}
    skip_extraction = unknown_answer or (payload.get('defer') and message == '销售暂未说明，先保留缺项。')
    result = {} if skip_extraction else client_factory().analyze(system_prompt=prompt, user_prompt=json.dumps({
        '已有条件': state['facts'], '刚才的问题': state['question']['text'], '本次用户原文': evidence_text,
    }, ensure_ascii=False))
    facts = dict(state['facts'])
    changed_fields = []
    for key, entry in (result.get('facts') or {}).items():
        if key not in FIELDS or not isinstance(entry, dict):
            continue
        quote, value = entry.get('quote'), entry.get('value')
        if key in {'scenario', 'requirements', 'recording'} and not requirement_quote_allowed(message, quote):
            continue
        if isinstance(quote, str) and quote.strip() and quote in evidence_text and isinstance(value, (str, int, float)) and str(value).strip():
            facts[key] = {'value': value, 'quote': quote, 'origin': 'user_statement'}
            changed_fields.append(key)
    def parameter_key(p):
        name = p['name']
        if '记录' in name or '导出' in name:
            return 'recording'
        return name
    parameters = {parameter_key(p): p for p in state.get('parameters', [])}
    for p in result.get('parameters') or []:
        if not isinstance(p, dict) or not isinstance(p.get('quote'), str) or not p['quote'].strip() or p['quote'] not in evidence_text:
            continue
        if not requirement_quote_allowed(message, p.get('quote')):
            continue
        if not isinstance(p.get('name'), str) or not p['name'].strip() or p.get('value') in (None, '') or p.get('direction') not in {'equal', 'min', 'max', 'covers'}:
            continue
        if p['direction'] == 'covers' and (not isinstance(p['value'], list) or len(p['value']) != 2 or not all(isinstance(v, (int, float)) for v in p['value']) or p['value'][0] > p['value'][1]):
            continue
        parameters[parameter_key(p)] = {k: p.get(k, '') for k in ('name', 'value', 'unit', 'direction', 'conditions', 'quote')}
    # The explicit recording requirement is one complete condition. Preserve it
    # even when the extractor omitted the duplicate parameters representation.
    if 'recording' in changed_fields:
        record = facts['recording']
        parameters['recording'] = {'name': '温度记录与导出' if '温度' in str(record['value']) else '记录与导出',
                                  'value': record['value'], 'unit': '', 'direction':'equal',
                                  'conditions':'', 'quote':record['quote']}
    deferred = list(state.get('deferred', []))
    if (payload.get('defer') or unknown_answer) and state['question']['group'] not in deferred:
        deferred.append(state['question']['group'])
    updated = {**state, 'facts': facts, 'parameters': list(parameters.values()), 'deferred': deferred,
               'revision': state['revision'] + 1, 'updated_at': datetime.now(timezone.utc).isoformat()}
    updated['question'] = next_question(updated)
    if changed_fields:
        acknowledged = '；'.join(f"{FIELDS[k]}：{facts[k]['value']}" for k in changed_fields)
        updated['reply'] = '已记录' + acknowledged + '。销售陈述会保留为待核实的采购条件。'
    else:
        updated['reply'] = '这次没有新增可确认的采购字段，已有条件仍保留。' if not skip_extraction else '这项先保留未知，我们继续。'
    updated['messages'] = state['messages'] if payload.get('initialize') else [*state['messages'], {'role': 'assistant', 'content': (state.get('reply') or '') + state['question']['text']}, {'role': 'user', 'content': message}]
    updated['draft'] = draft_baseline(updated)
    # Revision is checked again after the slow provider call.
    if read_intake(repo, task_id)['revision'] != state['revision']:
        raise ValueError('对话已有新内容，请刷新后继续。')
    # 这次回复返回的草稿也要带资料型号：否则刚发完一条消息，界面上预填的比较基准
    # 就退回口述型号，用户看到自己刚确认过的资料型号不见了。
    updated = with_document_suggestions(repo, task_id, updated)
    repo.put('procurement_records', updated['id'], updated, task_id=task_id)
    return updated
