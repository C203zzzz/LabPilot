"""Extracted procurement research helpers (candidate_gates)."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import re
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

from backend.domain.search import SearchQuery
from backend.schemas.search import source_to_dict
from backend.services.alternative_price_lookup import NOT_ATTEMPTED, NOT_REQUESTED
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_rules import (
    NO_CONFIRMED_PARAMETER,
    brand_from_source_line,
    brand_identity,
    comparison_basis,
    contains_model,
    evaluate_parameter,
    format_money,
    identity,
    model_spellings,
    supported,
    validate_price,
)
from backend.services.report_library import (
    empty_results_all_channels_text,
    empty_results_channel_text,
    search_channel_failed_text,
)
from backend.services.research_sources import is_tender, parse_web_url
from backend.services.source_authority import (
    host_of,
    is_local_authority_host,
    is_marketplace_or_aggregator,
    is_notice_page,
    product_page_rank,
)

from backend.services.research.constants import facade_attr
from backend.services.research.constants import BODY_CANDIDATE_LIMITS, CONFIGURATION_HEADINGS, CONFIGURATION_LEAD, CONFIGURATION_MAXIMUM, CONFIGURATION_MODEL_GATE_NAMES, CONFIGURATION_REFETCH_CANDIDATES_PER_RUN, CONFIGURATION_REFETCH_PAGES_PER_CANDIDATE, CONFIGURATION_SCOPE_PAGES, CONFIGURATION_WINDOW, ON_DEMAND_DEFAULT, ON_DEMAND_ENV, ON_DEMAND_TRUE, _ABBREVIATED_MODEL_RUN, _CONFIGURATION_MODEL_LINE, _CONFIGURATION_MODEL_LOOKBACK, _MEASURE_CELL_MARK, _MODEL_TOKEN_WITH_DOT, _NAME_VALUE_CELLS, _NOT_A_MODEL_LINE, _QUANTITY_WORD_CELL, _URL_IN_TEXT, _VALUE_SEPARATORS, _canonical_url
from backend.services.research.price_helpers import _stated
from backend.services.research.report_helpers import redact_error_text


def alternative_price_on_demand(environ=None):
    """按需查价开着没有。**每次调用都读一次环境**，不是一个 import 期常量。

    为什么不在 import 期读成模块常量：这个进程是长驻的研究服务，开关要能用一次
    重启（甚至一次 ``monkeypatch.setenv``）翻过来；import 期读死之后，测试里就只能
    改模块属性才能翻转，而"部署时设的环境变量"与"测试里改的属性"就成了两条路。
    读环境这一次开销可以忽略（一轮研究只读两次：车道开头一次、点按钮一次）。

    ``environ`` 只为测试注入：不传就是 ``os.environ``，行为与直接读一致。

    **没设这个环境变量时按 ``ON_DEMAND_DEFAULT``（开）算**；设了（哪怕是空串）就按
    设的那个值解析，所以显式关掉它这条路一直有效。
    """
    import os
    values = os.environ if environ is None else environ
    raw = values.get(ON_DEMAND_ENV)
    if raw is None:
        raw = ON_DEMAND_DEFAULT
    return str(raw).strip().lower() in ON_DEMAND_TRUE


def candidate_key(candidate, salt=''):
    base = identity((candidate or {}).get('model'))
    address = str((candidate or {}).get('url') or '').strip()
    if not address:
        address = '\x00no-url:' + str((candidate or {}).get('model') or '')
    return hashlib.sha256((base + address + salt).encode()).hexdigest()[:16]


def candidate_fallback_salt(candidate):
    """``url`` 为空、且 id 已经被同型号的另一个候选占走时的第二段地址。

    先试正文引文（同一轮两次算同一个值、与位置无关）；连引文都没有才用主机名，
    最后才回到空串（那种情况下这个候选与占位者确实无从区分，也只能给同一个 id——
    而这种情况在真实数据里不存在，见 ``candidate_key`` 的说明）。
    """
    excerpt = str((candidate or {}).get('excerpt') or '').strip()
    if excerpt:
        return '\x00dup:' + excerpt
    url = str((candidate or {}).get('url') or '').strip()
    if url:
        return '\x00dup:' + urlsplit(url).netloc
    return '\x00dup:'


def assign_candidate_ids(candidates, taken=None):
    """给还没有 ``candidate_id`` 的候选补上（已有的一律不动，重复跑是幂等的）。

    ``taken`` 是"这个 id 已经被占了"的集合，跨调用点共用：候选分两批落地
    （``alternatives`` 与 ``reference_candidates``），不共用的话两批之间还会撞。
    返回被写入 id 的候选列表。
    """
    claimed = taken if taken is not None else set()
    written = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict) or candidate.get('candidate_id'):
            continue
        key = candidate_key(candidate)
        if key in claimed:
            key = candidate_key(candidate, salt=candidate_fallback_salt(candidate))
            while key in claimed:
                # 同一个型号的两条重复候选、又没有引文可以区分：按"先到先得"把后
                # 一个往后挪，直到不撞。不用位置下标——位置一变 id 就变。
                key = hashlib.sha256((key + '\x00').encode()).hexdigest()[:16]
        candidate['candidate_id'] = key
        claimed.add(key)
        written.append(candidate)
    return written


def body_candidate_limit(purpose):
    """这条查询允许进正文的候选条数（**取值与改动前逐字相同**：5 / 3）。"""
    return BODY_CANDIDATE_LIMITS.get(purpose, BODY_CANDIDATE_LIMITS['default'])


def engine_query_for(provider, query):
    """The keyword string the provider really sent for this query.

    The recorded attempt has to match what was actually sent -- a telemetry
    field that says the model was searched while the engine received the
    category is exactly the kind of not-quite-true signal this run's alerts
    exist to remove. An adapter that walks several candidates (CCGP tries the
    exact model before falling back) reports the keyword that was really sent
    through ``last_engine_query``; one that sends a single keyword declares its
    arguments and is asked directly.
    """
    sent = getattr(provider, 'last_engine_query', None)
    if sent:
        return sent
    method = getattr(provider, 'engine_query', None)
    if method is None:
        return query.text
    if 'brand' in inspect.signature(method).parameters:
        return method(query.text, brand=query.brand, model=query.model)
    return method(query.text)


def confirmed_model(repo, task_id):
    """The instrument model the user confirmed for this task, if any.

    A stored candidate inventory becomes the procurement candidate list, so
    this is the one place that knows which model the user actually asked about.
    Only confirmed baselines count: the intake draft is itself derived from the
    analysis candidates, so filtering by it would be circular.
    """
    records = sorted([r for r in repo.list('procurement_records', task_id) if r.get('kind') == 'baseline'],
                     key=lambda r: r.get('created_at', ''))
    for record in reversed(records):
        model = str(record.get('model') or '').strip()
        if model:
            return model
    return None


def _table_of_contents_line(text, position):
    tail = text[position:position + 80]
    return tail.count('.') + tail.count('…') >= 8


def configuration_regions(text, model, window=CONFIGURATION_WINDOW, maximum=CONFIGURATION_MAXIMUM):
    """按标题取附件里的配置窗口，而不是从附件开头整段截断。

    短附件（不超过 ``maximum``）整份返回——那本来就是原来喂进去的全部内容。
    长附件只取配置类标题附近的窗口：窗口中同时出现目标型号的优先，其次按标题的
    具体程度，最后按出现位置；窗口之间不重叠，合计不超过 ``maximum``。

    标题一个都没命中时退回 ``text[:maximum]``。那一段不保证含配置，但改动前的行为
    就是这样，改成「什么都不喂」只会更差，所以退回而不是清空。
    """
    if not isinstance(text, str) or not text:
        return []
    if len(text) <= maximum:
        return [{'start': 0, 'text': text}]
    hits = []
    for rank, heading in enumerate(CONFIGURATION_HEADINGS):
        for match in re.finditer(re.escape(heading), text):
            if _table_of_contents_line(text, match.start()):
                continue
            start = max(text.rfind('\n', 0, match.start()) + 1, match.start() - CONFIGURATION_LEAD)
            excerpt = text[start:min(len(text), start + window)]
            hits.append(((0 if contains_model(excerpt, model) else 1), rank, match.start(), start, excerpt))
    if not hits:
        return [{'start': 0, 'text': text[:maximum]}]
    hits.sort(key=lambda hit: hit[:3])
    retained, spans, used = [], [], 0
    for _, _, _, start, excerpt in hits:
        if used >= maximum:
            break
        end = start + len(excerpt)
        if any(start < kept_end and end > kept_start for kept_start, kept_end in spans):
            continue
        excerpt = excerpt[:maximum - used]
        retained.append({'start': start, 'text': excerpt})
        spans.append((start, start + len(excerpt)))
        used += len(excerpt)
    retained.sort(key=lambda region: region['start'])
    return retained


def extract_config_locally(text, requirements):
    """本地正则+表格解析提取配置，作为模型抽取的兜底

    策略：
    1. 正则匹配：参数名：数值 单位
    2. 表格解析：识别配置清单表格

    返回：[{name, value, unit, source}, ...]
    """
    from backend.services.research_planning import parameter_labels, canonical_unit, as_number

    results = []

    # 策略 1：正则匹配 "参数名：数值 单位"
    for req in requirements:
        if not isinstance(req, dict) or not req.get('name'):
            continue

        labels = parameter_labels(req['name'])
        for label in labels:
            # 匹配：标签：数值 单位（支持中英文冒号）
            pattern = re.escape(label) + r'[：:]\s*([0-9.,~±≤≥-]+)\s*([a-zA-Z℃×°/()（）]+)?'
            match = re.search(pattern, text)
            if match:
                value_str = match.group(1).strip()
                unit_str = (match.group(2) or '').strip()

                # 尝试解析数值
                try:
                    # 处理范围值（取第一个数值）
                    if '~' in value_str or '-' in value_str:
                        # 范围值暂时跳过，因为不确定取哪个
                        continue

                    # 处理 ≤ ≥ ± 前缀
                    value_str = re.sub(r'^[≤≥±]\s*', '', value_str)

                    value = as_number(value_str)
                    if value is not None:
                        results.append({
                            'name': req['name'],
                            'value': value,
                            'unit': canonical_unit(unit_str) if unit_str else '',
                            'source': 'regex',
                            'excerpt': match.group(0)[:100],
                        })
                        break
                except (ValueError, TypeError):
                    continue

    # 策略 2：表格解析
    table_results = _parse_config_table(text, requirements)

    # 合并结果，去重（优先用正则匹配的）
    found_names = {r['name'] for r in results}
    for tr in table_results:
        if tr['name'] not in found_names:
            results.append(tr)

    return results


def _parse_config_table(text, requirements):
    """解析配置清单表格

    表格示例：
        序号  技术参数名称      技术要求           备注
        1     温度范围         -40℃~-86℃        主要指标
        2     有效容积         578 L             主要指标
    """
    from backend.services.research_planning import parameter_labels, canonical_unit, as_number

    results = []
    lines = text.splitlines()

    # 识别表格区域（通常在"配置清单"之后）
    in_table = False

    for i, line in enumerate(lines):
        # 检测表格开始
        if any(heading in line for heading in ['配置清单', '技术参数', '主要参数', '规格参数']):
            in_table = True
            continue

        # 检测表格结束（空行或新章节）
        if in_table and (not line.strip() or any(end in line for end in ['三、', '四、', '注：', '备注：'])):
            in_table = False
            continue

        if not in_table:
            continue

        # 匹配要求中的参数名
        for req in requirements:
            if not isinstance(req, dict) or not req.get('name'):
                continue

            param_name = req['name']
            if param_name not in line:
                continue

            # 在同一行或下一行提取值
            value_line = line
            if i + 1 < len(lines):
                value_line += ' ' + lines[i + 1]

            # 匹配数值+单位
            match = re.search(r'([0-9.,~±≤≥-]+)\s*([a-zA-Z℃×°/()（）]+)?', value_line[value_line.find(param_name):])
            if match:
                value_str = match.group(1).strip()
                unit_str = (match.group(2) or '').strip()

                try:
                    # 处理范围值（取第一个数值）
                    if '~' in value_str:
                        value_str = value_str.split('~')[0].strip()

                    # 处理前缀
                    value_str = re.sub(r'^[≤≥±]\s*', '', value_str)

                    value = as_number(value_str)
                    if value is not None:
                        results.append({
                            'name': param_name,
                            'value': value,
                            'unit': canonical_unit(unit_str) if unit_str else '',
                            'source': 'table',
                            'excerpt': line.strip()[:100],
                        })
                        break
                except (ValueError, TypeError):
                    continue

    return results


def excerpt_with_model(body_text, model):
    """从这一条来源的正文里就地找一行**含候选型号**的原文行；找不到返回 ``''``。

    这是"引文不合格时就地补引文"那一步（``output/c-drift/report.md`` §2.1）：c4 那 5 条
    模型候选的引文引的是尺寸表行，一个字都不含型号，而它们的来源正文里**本来就有**含型号
    的行（5 条里 4 条当场就能找到）。纯本地字符串遍历，零网络、零模型调用。

    识别用的是候选核验入口那一层（``candidate_source_matches_model``：型号本体，或"型号 +
    连字符后缀"这种规范写法，见 ``research_planning`` 里那一段说明），比 ``contains_model``
    略宽——补出来的引文必须能过闸门，用更严的口径去找会白找。
    """
    if not isinstance(body_text, str) or not str(model or '').strip():
        return ''
    from backend.services.research_planning import candidate_source_matches_model
    for line in body_text.splitlines():
        if candidate_source_matches_model(line, model) and not _NOT_A_MODEL_LINE.search(line):
            return line
    return ''


def gate_discovered_candidates(raw_candidates, sources, base_model):
    """发现模型那一批候选的**唯一入口闸门**：引文不合格先就地补，补不到才丢这一条。

    改之前这里是一句列表推导（``:5047-5049``）：要求"候选的引文里必须出现该候选的型号"，
    不满足就把这次调用的输出**整批**拒收，连"正文里有含型号行、只是引文引错了"的候选也
    一起丢。代价实测（``output/c-drift/report.md`` §2.1）：c／c2 引型号表行 → 6/6 全过；
    c4 引尺寸表行 → 5/5 全灭，整轮候选池从 5 条塌成 1 条，而**唯一**的差别是模型那一次
    调用摘了哪一行。

    现在逐个候选判，三种结果：

    * **过闸门**：URL 命中这条来源、引文逐字在正文里、引文里认得出候选型号；
    * **就地补**：引文里没有型号，但**这条候选自己那条来源**的正文里有含型号的行，
      用那一行换掉引文（只换引文，型号和来源地址不动；不跨来源借正文）；
    * **丢一条**：引文在自己的来源正文里逐字找不到，或正文里确实没有含型号的行。
      丢的是**这一条**，不是整批；调用方按 ``设计规范`` §5 留痕（``gaps`` 说人话、
      ``failures`` 带原文与位置）。

    返回 ``(discovered, repaired, dropped)``：``discovered`` 是过了闸门的候选副本，
    ``repaired`` 是被补过引文的型号列表，``dropped`` 是丢掉那几条的事实（型号／地址／
    原引文／原因码），供调用方留痕。
    """
    from backend.services.research_planning import candidate_source_matches_model
    discovered, repaired, dropped = [], [], []
    for candidate in list(raw_candidates or [])[:10]:
        if not isinstance(candidate, dict) or not candidate.get('model'):
            continue
        model = candidate['model']
        if identity(model) == identity(base_model):
            continue
        source = next((s for s in sources or [] if s.get('url') == candidate.get('url')), None)
        if source is None or not supported(candidate.get('excerpt'), source.get('body_text')):
            # 引文在被引用的正文里逐字找不到（编造或抄错页）：这一条不能用，也不补——
            # 它连自己引的是哪一页都没对上，替它找一行等于把"抄错了"洗成"抄对了"。
            dropped.append({'model': model, 'url': (source or {}).get('url') or str(candidate.get('url') or ''),
                            'excerpt': str(candidate.get('excerpt') or '')[:200],
                            'reason': 'excerpt_not_in_source'})
            continue
        if not candidate_source_matches_model(candidate.get('excerpt', ''), model):
            repaired_line = excerpt_with_model(source.get('body_text'), model)
            if not repaired_line:
                dropped.append({'model': model, 'url': source.get('url') or '',
                                'excerpt': str(candidate.get('excerpt') or '')[:200],
                                'reason': 'excerpt_without_model'})
                continue
            candidate = {**candidate, 'excerpt': repaired_line}
            repaired.append(model)
        discovered.append(copy.deepcopy(candidate))
    return discovered, repaired, dropped


def merge_discovered_candidates(discovered, scanned):
    """模型提出的候选与规则直读的线索**始终合并去重**（``设计规范`` §九之三）。

    改之前规则兜底只在模型返回空数组时接管（``if not run['discovered_candidates']``），
    于是"模型给了候选、但给得不好"的那些轮里，正文里明明存在的线索一条也补不进来
    （``output/c-drift/report.md`` §2.2：兜底本身是确定性的、每轮都稳定捞出 10 条，
    只是没机会跑）。两个来源的产出**结构相同**，下游（``triage``／``configuration_alignment``
    ／详情页／名额分配）一视同仁。

    去重键是型号的规范化写法（``identity``，保留后缀与数字）：模型提过的型号，正文行再读到
    一次时只留模型那条（它带着 ``match_basis`` 等模型字段，信息更多）。规则线索排在
    **模型候选后面**——名额分配按顺序走，先给解释更完整的那一批。

    返回 ``(merged, added)``：``added`` 是这次真的新补进来的线索。
    """
    def stable_key(candidate):
        return (str(_canonical_url(candidate.get('url')) or ''),
                identity(candidate.get('brand')), identity(candidate.get('model')),
                str(candidate.get('excerpt') or ''),
                str(candidate.get('proposed_by') or ''))

    merged = []
    seen = set()
    for candidate in sorted((c for c in (discovered or []) if isinstance(c, dict)), key=stable_key):
        key = identity(candidate.get('model'))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(candidate)
    added = []
    for candidate in sorted((c for c in (scanned or []) if isinstance(c, dict)), key=stable_key):
        if not candidate.get('model'):
            continue
        key = identity(candidate['model'])
        if not key or key in seen:
            continue
        seen.add(key)
        added.append(candidate)
    return merged + added, added


def nearest_model_spellings(sources, model, limit=3):
    """这一轮的页面里出现的、与 ``model`` **同族但写法不同**的型号串（最多 ``limit`` 个）。

    只用来**如实说明为什么没接上**，绝不拿来当匹配用——把页面上的 ``CC-1008E`` 认成候选
    ``CC-1008EW``，就是把别的机器的参数挂到这台头上（``设计规范`` 有意收紧的完整 token 口径，
    不许放宽）。判"同族"只看前 4 个规范化字符相同、整串不同：这在实测里正好落在
    ``CC-1008EW``/``CC-1008E``、``H1650K``/``H-1650``、``VS-840U``/``VS-840-2`` 这些例子上。
    """
    from backend.services.research_planning import _MODEL_TOKEN
    want = identity(model)
    if not want:
        return []
    head = want[:4]
    found = []
    for source in sources or []:
        if not isinstance(source, dict) or source.get('body_status') != 'fetched':
            continue
        for token in _MODEL_TOKEN.findall(source.get('body_text') or ''):
            key = identity(token)
            if not key or key == want or len(key) < len(head) or key[:4] != head:
                continue
            if token not in found:
                found.append(token)
            if len(found) >= limit:
                return found
    return found


def candidate_verification_source(candidate, sources):
    """这条候选在**本车道取回的那批来源**里，有没有一条能当核验依据的原文页。

    核验入口那三条判据的唯一实现（``alternatives()`` 的两条分支、以及"争核验名额"的排序键
    都调这里）：

    1. 候选那一行给出的地址要在这批来源里——``sources`` 是车道开头由 ``discovery_sources``
       定下的**快照**，之后才抓回来的详情页不在里面；
    2. 引文要能在该页正文里**逐字**读到（``supported``）；
    3. 引文里要认得出这个型号（``candidate_source_matches_model``，不是 ``contains_model``：
       后者要求完整 token，见那两处各自的说明）。

    三条任一条不过就返回 ``None``——**这个候选过不了核验入口**。

    第一条认**两个**地址：``url`` 与 ``discovery_url``。2026-10-06 起候选的主链接被对齐到
    "抽配置的那一页"（见 :func:`align_candidate_source_url`），发现它的那一页挪进
    ``discovery_url``；引文本来就是从发现页抄下来的，只认新地址会让这条候选在自己的引文
    面前变成"来源不可用"。两个地址都是这条候选自己的页面，判据一条都没放宽：地址要在这批
    来源里、引文要逐字读得到、型号要认得出来，三条照旧。

    为什么排序也要用同一把尺子（2026-10-05 真跑实测，``b3-r2-local-leads``）：本地成交明细库
    补进来的线索，地址是 8093 的成交页，而 ``sources`` 那份快照里没有它，所以它们**结构上
    永远过不了闸门**。上一版排序键只看配置、品牌、地址字符串，而 8093 的地址以
    ``https://gdegp`` 开头、字典序很靠前，于是这 6 条线索把 **3 个核验名额全占了**，
    又一个都过不了闸门——那一轮的 ``alternatives`` 直接空掉（同一套样本的其它 5 轮都是 2~3 条）。
    名额给不了能核验的候选，等于把这一轮能出的结论全扔掉。
    """
    from backend.services.research_planning import candidate_source_matches_model
    urls = {str(candidate.get('url') or ''), str(candidate.get('discovery_url') or '')}
    urls.discard('')
    source = next((item for item in (sources or [])
                   if isinstance(item, dict) and item.get('url') in urls), None)
    if source is None:
        return None
    if not supported(candidate.get('excerpt'), source.get('body_text')):
        return None
    if not candidate_source_matches_model(candidate.get('excerpt', ''), candidate.get('model')):
        return None
    return source


def _parameter_value_key(value):
    """参数值／引文的比较键：NFKC、去空白、大小写不敏感、尺寸分隔符归一。"""
    text = unicodedata.normalize('NFKC', str(value if value is not None else '')).casefold()
    text = re.sub(r'\s+', '', text)
    return _VALUE_SEPARATORS.sub('*', text)


def _parameter_evidence_excerpts(parameter):
    """这一项参数自带的引文（分析抽取的 ``sources[].excerpt``，或单条 ``excerpt``）。"""
    excerpts = []
    for source in parameter.get('sources') or []:
        if isinstance(source, dict) and str(source.get('excerpt') or '').strip():
            excerpts.append(str(source['excerpt']))
    single = parameter.get('excerpt')
    if isinstance(single, str) and single.strip():
        excerpts.append(single)
    return excerpts


def parameter_value_gap(parameter):
    """逐字校验"这一项的值 ↔ 它自己的引文"；通过（或没有引文可校）返回 ``None``。

    判据只有一条：值（``covers`` 那种两端的范围值逐个端）必须能在**它自己那条引文**里
    逐字读到，空白与尺寸分隔符写法不算差异。
    """
    if not isinstance(parameter, dict):
        return None
    excerpts = _parameter_evidence_excerpts(parameter)
    if not excerpts:
        # 没有引文 → 这一层无从校验（值来自用户自己填写或外部脚本直接提交），原样保留。
        return None
    value = parameter.get('value')
    parts = list(value) if isinstance(value, (list, tuple)) else [value]
    haystack = [_parameter_value_key(excerpt) for excerpt in excerpts]
    for part in parts:
        key = _parameter_value_key(part)
        if not key or not any(key in text for text in haystack):
            return {'name': str(parameter.get('name') or ''), 'value': value, 'excerpts': excerpts}
    return None


def parameters_with_verbatim_evidence(parameters):
    """写进比较基准之前，逐项做"参数值 ↔ 它自己的引文"的逐字校验。

    为什么卡这一道（``output/c-drift/report.md`` §2.1、§五.4）：c 与 c4 两轮的比较基准里
    都带着一条 ``工作室尺寸``，值却不一样（``600x550x750`` 对 ``400x400x500``，原文那两行
    分别属于同一页上不同的型号）。分析那一层校验的是"这个值能在**这一页正文**里找到"，
    同一页上别的型号那一行照样能过；到了"写进比较基准"这一步，就没有第二道了。宁可少一项
    （c2 那轮只有三项，照样跑完），也不拿对不上自己引文的值当确定值。

    口径（``设计规范`` §3.4／§5）：**跳过这一项、不丢数据**——``gaps`` 写人话（哪一项、
    什么值、为什么没写），``failures`` 写排查用的原文与位置；同一件事只在这两处各说一次。

    返回 ``(kept, gaps, failures)``。``kept`` 是**原对象**，只做筛选、不复制、不改字段。
    """
    kept, gaps, failures = [], [], []
    import traceback
    frame = traceback.extract_stack()[-2]
    for parameter in parameters or []:
        gap = parameter_value_gap(parameter)
        if gap is None:
            kept.append(parameter)
            continue
        name = gap['name'] or '未命名指标'
        value_text = str(gap['value'])
        gaps.append('%s：这一项的值 %s 在它自己的原文引文里找不到，未写进比较基准'
                    '（宁可不写这一项，也不拿对不上引文的值当确定值）。' % (name, value_text))
        excerpts = gap['excerpts']
        first = excerpts[0] if excerpts else ''
        failures.append({
            'stage': 'baseline_parameters',
            'type': 'ParameterValueNotVerbatim',
            'code': 'parameter_value_unverified',
            'location': {'function': frame.name, 'line': frame.lineno,
                         'file': frame.filename.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]},
            'name': name,
            'value': value_text,
            'excerpt_count': len(excerpts),
            'error': redact_error_text('参数值 %s 在它自己的引文里找不到；引文：%s'
                                       % (value_text, first[:400])),
            'note': '这一项没有写进比较基准，其余项不受影响；type 是这条留痕的类别，'
                    '不是抛出来的异常类。',
        })
    return kept, gaps, failures


def candidate_reject_failure(item):
    """候选被入口闸门跳过时的排查留痕（``run['failures']`` 里的一条，与那一节同形）。

    ``type`` 是这条留痕的**类别**（``CandidateExcerptRejected``），不是抛出来的异常类——
    "引文里认不出这个型号"是证据不合格，不是程序故障；编一个异常类名等于把"抄错了行"
    说成"程序崩了"（同一形状的先例见 ``deepseek_analysis._brand_unresolved_failure``）。
    ``error`` 里的原引文先过 ``redact_error_text``：留痕要能定位到具体哪一条候选，
    但不能把引文里可能带的号码或凭据一起落库。
    """
    import traceback
    frame = traceback.extract_stack()[-2]
    return {
        'stage': 'candidate_discovery',
        'type': 'CandidateExcerptRejected',
        'code': item['reason'],
        'location': {'function': frame.name, 'line': frame.lineno,
                     'file': frame.filename.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]},
        'model': item['model'],
        'url': item['url'],
        'error': redact_error_text('候选 %s 的引文在它自己引用的来源正文里认不出这个型号；'
                                   '原引文：%s' % (item['model'], item['excerpt'])),
        'note': '这一条候选已跳过（不是整批拒收）；type 是这条留痕的类别，不是抛出来的异常类。',
    }


def candidate_source_pages(candidate):
    """这条候选能追溯到哪些来源网页（列表；主来源是发现它的那一页）。

    只读候选上**已经写好的**字段，不新增抓取、不猜地址：候选的 ``url``、
    ``detail_page``、``brand_evidence``、``config_match.items[].observed.evidence``、
    ``matrix[].observed/observations/raw_evidence``。空地址一律不产出条目——"没有地址"
    与"有地址但不可点"是两件事，前者不该在页面上变成一行"来源未提供"。

    同一个网址只留一条（先出现的那步算它的来路）：同一条详情页既是品牌出处又是配置
    证据时，页面上一行就够，印两行读起来像两条不同的来源。
    """
    if not isinstance(candidate, dict):
        return []
    rows = []
    seen = set()

    def usable_url(node):
        """旧证据可能把真实地址留在 original_url，不能把 # 传到来源卡片。"""
        if not isinstance(node, dict):
            return node
        url = node.get('url')
        return node.get('original_url') if url == '#' and node.get('original_url') else url

    def add(url, origin, primary=False):
        url = str(url or '').strip()
        if not url or url in seen:
            return
        seen.add(url)
        rows.append({'url': url, 'origin': origin, 'primary': bool(primary)})

    def add_evidence(node, origin):
        """``{'evidence': {'url': ...}}`` 与 ``{'url': ...}`` 两种形状都收。"""
        if not isinstance(node, dict):
            return
        evidence = node.get('evidence') or {}
        add(usable_url(evidence), origin)
        add(usable_url(node), origin)

    add(usable_url(candidate), 'discovered', primary=True)
    add(usable_url(candidate.get('detail_page') or {}), 'detail_page')
    add(usable_url(candidate.get('brand_evidence') or {}), 'brand_evidence')
    add(usable_url(candidate.get('supplier_evidence') or {}), 'supplier_evidence')
    for item in ((candidate.get('config_match') or {}).get('items') or []):
        if isinstance(item, dict):
            add_evidence(item.get('observed'), 'config_match')
    for row in (candidate.get('matrix') or []):
        if not isinstance(row, dict):
            continue
        add_evidence(row.get('observed'), 'matrix')
        for observation in (row.get('observations') or []):
            add_evidence(observation, 'matrix')
        for evidence in (row.get('raw_evidence') or []):
            add_evidence(evidence, 'matrix')
    return rows


def record_candidate_source_pages(candidate):
    """把这条候选的来源网页写回候选自己（``candidate['source_pages']``），返回写完的列表。

    两条硬要求：

    * **只增不减**——同一候选在不同阶段被不同来源支撑（发现 → 详情页 → 品牌 →
      配置 → 参数），每次都调一次，结果取并集。设计规范"取信息最全的那次"就是这一步：
      重跑一轮只拿到 1 条来源时，上一轮那 3 条**一个字都不删**（``设计规范`` §5
      "内部事实不许被抹掉"）。已有条目的 ``origin`` / ``primary`` 一律不因为后一次
      调用而改写，只在必要时补上主来源标记。
    * **主来源最多一条**——它由"发现这条候选的那一页"（``candidate['url']``）唯一决定；
      没有那一页时一条都不标（宁可不标，也不拿别的页冒充主来源）。

    非字典、或没有型号的条目不是卡片（调用点与 ``note_price_lookup_not_attempted``
    同一个容错口径），原样返回、不写字段。
    """
    if not isinstance(candidate, dict) or not candidate.get('model'):
        return list(candidate.get('source_pages') or []) if isinstance(candidate, dict) else []
    merged, seen = [], set()
    for row in list(candidate.get('source_pages') or []) + candidate_source_pages(candidate):
        if not isinstance(row, dict):
            continue
        url = str(row.get('url') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append({'url': url, 'origin': str(row.get('origin') or ''),
                       'primary': bool(row.get('primary'))})
    main = str(candidate.get('url') or '').strip()
    if main == '#':
        main = str(candidate.get('original_url') or '').strip()
    for row in merged:
        row['primary'] = bool(main) and row['url'] == main
    if merged:
        candidate['source_pages'] = merged
    return merged


def candidate_configuration_url(candidate):
    """Which page this candidate's configuration was actually read from ('' if none).

    Two fields carry that answer and both have to be read: ``configuration_url``
    is written where the configuration lands (``search_candidate_product_pages``
    and ``product_configuration`` both write it), and older candidates that
    predate the field fall back to ``detail_page``. Only a page that really
    yielded a parameter block counts there (``coverage == 'parameters'``):
    ``model_only`` is a page that merely spells the model, which is not where a
    configuration came from.
    """
    if not isinstance(candidate, dict):
        return ''
    url = str(candidate.get('configuration_url') or '').strip()
    if url:
        return url
    detail = candidate.get('detail_page') or {}
    if isinstance(detail, dict) and detail.get('coverage') == 'parameters':
        return str(detail.get('url') or '').strip()
    return ''


def align_candidate_source_url(candidate):
    """Point a candidate's main link at the page its configuration came from.

    The measured defect (``output/e2e-final/runs/b3-opt-cleanbench-boxun-bj1cd``,
    ``02-判定暴露的问题.md`` §Q6): candidate ``bekomat30`` carried
    ``url = olaibo.com/ProductDetail/634.html`` (that page is the ``BBS-H1300``)
    while ``configuration_url = .../630.html`` (that page is the ``BBS-V600``).
    The link on the card and the page the system read were two different pages,
    so what the user opened and what the system judged could not be compared --
    the user withdrew an earlier "真可比" verdict over exactly this.

    What changes is **which page the main link points at**, never the
    configuration or its provenance: the configuration text, ``configuration_url``
    and the excerpt are untouched. The discovering page is kept in two places --
    ``discovery_url`` and the ``origin='discovered'`` entry in ``source_pages`` --
    so "where was this candidate seen" is not lost, and
    ``candidate_verification_source`` accepts both addresses (they are both this
    candidate's own pages), which keeps the verification gate exactly as strict as
    it was.

    ``candidate_id`` is deliberately not recomputed here: it was already derived
    from the discovering URL and written onto the card, so the "look up this
    candidate's price" call, which locates the card by ``candidate_id``, still
    finds it. Returns whether the link moved.
    """
    if not isinstance(candidate, dict) or not candidate.get('model'):
        return False
    target = candidate_configuration_url(candidate)
    current = str(candidate.get('url') or '').strip()
    if not target or _canonical_url(target) == _canonical_url(current):
        return False
    if current and current != '#':
        candidate.setdefault('discovery_url', current)
    # 先把发现页定稿进来源清单（``record_candidate_source_pages`` 只增不减），再挪链接、
    # 再重算主来源。顺序反过来的话，"发现这条候选的那一页"在还没被记录过时就会从
    # 来源清单里消失——那正是这次改动要保住的东西。
    record_candidate_source_pages(candidate)
    candidate['url'] = target
    candidate['source_url_alignment'] = {
        'from': current,
        'to': target,
        'reason': '候选卡片上的详情页链接必须就是抽配置的那一页；发现页地址保留在 discovery_url 与来源清单里。',
    }
    record_candidate_source_pages(candidate)
    return True


def _candidate_configuration_observations(candidate):
    """The configuration *values* this candidate actually read, from both tables.

    A candidate card shows its configuration in more than one place: the
    ``configuration`` text block, and the per-requirement rows
    (``config_match.items`` -- what the candidate's own detail page states) plus
    the cross-brand parameter matrix (``matrix``). A candidate whose text block is
    empty but whose rows carry real readings is **not** "no configuration": the
    owner read exactly those rows on the 华志 ``PTY-124/223`` card and said the
    machine is in fact comparable ("其实是符合的").

    ``_stated`` decides what counts as a reading, so ``unknown``/``未知`` and
    blanks state nothing -- a row of eight ``未取得证据`` rows is not a
    configuration, which is what the 灭菌器 ``HSA-1500M`` / ``MST-H 9-6-18 HS2``
    cards were (8 alignment rows, 0 values).
    """
    values = []

    def collect(node):
        if not isinstance(node, dict):
            return
        observed = node.get('observed')
        if isinstance(observed, dict) and _stated(observed.get('value')) is not None:
            values.append(observed.get('value'))
        for item in (node.get('observations') or []):
            if isinstance(item, dict) and _stated(item.get('value')) is not None:
                values.append(item.get('value'))
        for item in (node.get('raw_evidence') or []):
            if isinstance(item, dict) and _stated(item.get('raw_value')) is not None:
                values.append(item.get('raw_value'))

    if not isinstance(candidate, dict):
        return values
    for item in ((candidate.get('config_match') or {}).get('items') or []):
        collect(item)
    for row in (candidate.get('matrix') or []):
        collect(row)
    return values


def candidate_has_configuration(candidate):
    """Whether this candidate has any configuration to put on a card.

    Two ways to have one, and either is enough:

    * ``configuration`` carries text -- that is the field the card's
      configuration block renders (the same field name the price cards use);
    * a per-requirement row carries a reading (see
      :func:`_candidate_configuration_observations`).

    Requiring the text block alone would withdraw cards the owner judged on their
    merits -- ``PTY-124/223``'s block is empty while its parameter matrix holds
    the 120/220 readings he called "其实是符合的".
    """
    if not isinstance(candidate, dict):
        return False
    text = candidate.get('configuration')
    if isinstance(text, str) and text.strip():
        return True
    return bool(_candidate_configuration_observations(candidate))


def _expanded_abbreviations(text):
    """把共前缀缩写展开回完整写法（``BW-05H/10H/20H`` → ``BW-10H``、``BW-20H``）。

    缩写段按前一个 token 的**字母前缀**补：``BW-05H`` 的第一个数字在 ``-`` 后面，前缀就是
    ``BW-``，于是 ``10H`` → ``BW-10H``（``LS-35LD/50LD`` → ``LS-50LD`` 同理）。
    只补**以数字开头**的段：以字母开头的段（``…/LS-75HV``、``…/B75II``）自带字母头，
    补了就是猜（``B75II`` 该补成 ``BKQ-B75II`` 还是本来就是 ``B75II``，从这一行看不出来），
    这类段照旧由扫描器按原样认。
    """
    expanded = []
    for match in _ABBREVIATED_MODEL_RUN.finditer(_URL_IN_TEXT.sub(' ', str(text or ''))):
        head = match.group(1)
        first_digit = next((index for index, character in enumerate(head)
                            if character.isdigit()), None)
        if first_digit is None:
            continue
        stem = head[:first_digit]
        for segment in match.group(2).strip('/').split('/'):
            if segment and segment[0].isdigit():
                expanded.append(stem + segment)
    return expanded


def model_tokens_in_line(text, strict=False):
    """一行里的型号写法：**逐字写着**的那些 ＋ 共前缀缩写展开出来的那些。

    展开出来的写法不在这行里逐字连着出现（``BW-10H`` 在页面上是 ``BW-05H/10H/20H``），
    所以只有"配置所属型号"这一格用它——回捞门槛（:func:`model_tokens_in_text`）仍只看
    逐字写着的那一份，免得把缩写展开带到别的判定里去。展开出来的写法同样过
    ``_is_plausible_model`` 与 ``strict`` 那把尺子（借 :func:`model_tokens_in_text` 过）。
    """
    found = list(model_tokens_in_text(text, strict))
    expanded = _expanded_abbreviations(text)
    if not expanded:
        return found
    for token in model_tokens_in_text(' '.join(expanded), strict):
        if any(identity(token) == identity(item) for item in found):
            continue
        found.append(token)
    return found


def _spelling_of_the_same_model(token, model):
    """页面上这个写法与候选是不是**同一台机器**；是的话返回该记哪个写法。

    后缀写法（``BXM-30R双列六孔`` 的中文说明、``B-260型`` 的「型」）不该在卡片上显示成
    "配置属于另一个型号"——那是同一台机器的两种写法。判"同一台"复用既有的两把工具：
    :func:`procurement_rules.identity` ＋ :func:`procurement_rules.model_spellings`。

    不是同一台 → 原样返回页面上的写法（``bekomat30`` 那条：页面写 ``BBS-V600``，
    与候选不是同一台，必须原样留着）。是同一台但写法不同 → 记**候选自己的写法**，
    这样归一后两边相同，展示层按"同一件事不重复说"不显示这一格（``frontend/src/
    services/configurationModel.js``）。**这不是"拿候选顶上"**：页面这一行确实写着这台
    机器，只是后缀写法不同；页面没写这台机器时（一个都对不上）这里不会命中。
    """
    token = str(token or '').strip()
    candidate = str(model or '').strip()
    if not token or not candidate:
        return token
    if identity(token) == identity(candidate):
        return token
    left = {identity(spelling) for spelling in model_spellings(token) if spelling}
    right = {identity(spelling) for spelling in model_spellings(candidate) if spelling}
    left.discard('')
    right.discard('')
    if left & right:
        return candidate
    return token


def model_tokens_in_text(text, strict=False):
    """这段文本里**逐字写着**、且形状像型号的写法（按出现顺序去重）。

    复用价格那边已有的那把尺子（``price_rejections._is_plausible_model``）：只认
    "字母+数字、长度够、不是 ``50ml``／``6min``／尺寸／项目编号那种噪声"的写法，
    **不新造型号词表**。``_is_plausible_model`` 会排除"与目标型号相同"的那一个，这里传
    空串当目标——要认的恰恰是配置属于谁，候选自己那个型号也算数。

    扫描用的字符集与 ``price_rejections._MODEL_TOKEN`` 同一个形状，多一个点号与罗马数字
    （见 :data:`_MODEL_TOKEN_WITH_DOT`）：``LMQ.C-80E``、``BXM-170V-MⅡ`` 这类写法要整体
    认出来，不许在点号／罗马数字处断开。判据本身仍是 ``_is_plausible_model``，没有放宽。
    一行里的共前缀缩写（``BW-05H/10H/20H``）不在这一层展开——那是"配置所属型号"这一格
    才需要的，走 :func:`model_tokens_in_line`；回捞门槛仍用这一个函数（只看逐字写着的那份）。

    网址整段先抹掉（两种用法都要）：``[https://…/product/show-6052.html]`` 里的
    ``show-6052`` 实测被当成了"配置所属型号"。

    ``strict=True`` 再加一条**只给"配置所属型号"这一格用**的过滤：数字比字母多的写法不算
    型号（``200-900rpm`` 转速、``RS485`` 接口、``900Lx`` 照度都是这个形状）。
    **回捞的门槛不能用 strict**：``XQ-500``／``ASK350`` 这类真型号的数字也比字母多，
    严格版会把它们挡在回捞门外（实测：误用了 strict 之后回捞救回数从 149 掉到 102）。
    """
    from backend.services.price_rejections import _is_plausible_model
    cleaned = _URL_IN_TEXT.sub(' ', str(text or ''))
    found = []
    for token in _MODEL_TOKEN_WITH_DOT.findall(cleaned):
        if not _is_plausible_model(token, ''):
            continue
        if strict and sum(character.isdigit() for character in token) \
                > sum(character.isalpha() for character in token):
            continue
        if any(identity(token) == identity(item) for item in found):
            continue
        found.append(token)
    return found


def _one_model_of_the_candidate(tokens, model):
    """一行里写着多个型号时，挑出与**候选自己**是同一个的那一个：``(型号, 原因)``。

    多列表格（一页四个型号对照）与多型号对照行不能取第一列——实测鑫贝西
    ``BKQ-B75II`` 那张表，四列的技术参数值一模一样（``设计温度 | 150℃ | 150℃ | 150℃ |
    150℃``），"值落在第几格"根本定不了列，旧代码取到表头第一格 ``BKQ-B50II``。这一行
    里就有候选自己（``BKQ-B75II``），该取它。

    判"是同一个"用项目里既有的两把工具，**不新造一套**：

    * :func:`procurement_rules.identity`——大小写、连字符、全角半角归一（``NFKC``，
      所以 ``BXM-170V-MⅡ`` 与 ``…-MII`` 是同一个）；
    * :func:`procurement_rules.model_spellings`——型号后面粘着的**中文说明**
      （``BXM-30R双列六孔`` → ``BXM-30R``）。

    一个都对不上（或对上不止一个）时返回 ``(None, 原因)``：**不取第一个，也不拿候选
    自己的型号顶上**——留空是可以接受的，错的值不行。

    "对上不止一个"眼下不会发生（:func:`model_tokens_in_text` 已经按 ``identity`` 去过重，
    同一个型号在一行里写两遍只会剩一个 token），这条判断留着是别让将来的改动悄悄把它变成猜。
    """
    candidate = str(model or '').strip()
    listed = '、'.join(tokens[:4])
    if not candidate:
        return None, '这一行里写着多个型号（%s），判不出这一份配置属于谁' % listed
    wanted = {identity(spelling) for spelling in model_spellings(candidate) if spelling}
    wanted.discard('')
    matched = [token for token in tokens if identity(token) in wanted]
    if len(matched) == 1:
        return matched[0], ''
    if not matched:
        return None, ('这一行有多个型号（%s），但都不是本候选（%s），判不出这一份配置属于谁'
                      % (listed, candidate))
    return None, ('这一行有多个型号（%s），其中有多个与本候选（%s）是同一个，'
                  '判不出这一份配置属于谁' % (listed, candidate))


def configuration_model_from_configuration(configuration_text, model=''):
    """这份配置文本**自己声明**的型号：``(model, 那一行原文)`` 或 ``(None, 原因)``。

    只认**行首写着"型号"的那一行**（``型号 技术参数 | BBS-V600``、``型号技术参数：BCV-3S1``、
    ``规格型号：BCV-3S1``）：那是页面自己声明的，恰好一个型号 → 就是它（写法只差后缀时记
    候选自己的写法，见 :func:`_spelling_of_the_same_model`）；写着多个 → 只有
    **与候选自己是同一个**的那一个能被认（:func:`_one_model_of_the_candidate`），
    都不是就留空。共前缀缩写行（``型号 BW-05H/10H/20H``）按
    :func:`model_tokens_in_line` 展开后再判，展开后一个都对不上同样留空。

    **不在整段参数里捞型号。** 试过一版"整段恰好一个型号就用它"，结果 219 字那段贝茵配置里
    的 ``AC220V``（电源）被当成了型号——参数段里到处是这种"字母+数字"的形状
    （``AC220V``／``250VA``／``900Lx``，那一段里就有三个），在整段里捞就是猜。
    这一行没有可用的型号时返回 ``None``，交给 :func:`configuration_model_from_page`
    按"与这份配置同行／同列"去判；两层都判不出就留空。
    """
    for line in str(configuration_text or '').splitlines():
        if not _CONFIGURATION_MODEL_LINE.match(line):
            continue
        line_tokens = model_tokens_in_line(line, strict=True)
        if len(line_tokens) == 1:
            return _spelling_of_the_same_model(line_tokens[0], model), line.strip()
        if len(line_tokens) > 1:
            matched, detail = _one_model_of_the_candidate(line_tokens, model)
            if matched:
                return matched, line.strip()
            return None, detail
    return None, '配置文本里没有一行以"型号"开头（参数里的 AC220V／250VA 这类形状不算型号）'


def _split_cells(line):
    """一行的单元格：``(起始下标, 结束下标, 文本)``。

    竖线／全角竖线／制表符是主分隔符；跨过它们之后剩下的段里再按两个以上空格细分
    （对齐式表格就是这么排的）。下标保留在原行坐标系里，好跟另一行的格做区间重叠。
    """
    cells = []
    for chunk in re.finditer(r'[^|\t｜]+', line):
        text = chunk.group(0)
        if '  ' not in text:
            stripped = text.strip()
            if stripped:
                offset = text.index(stripped)
                cells.append((chunk.start(0) + offset, chunk.start(0) + offset + len(stripped),
                              stripped))
            continue
        for piece in re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|$)', text):
            cells.append((chunk.start(0) + piece.start(0), chunk.start(0) + piece.end(0),
                          piece.group(0).strip()))
    return cells


def configuration_model_from_page(page_body, configuration_text, model=''):
    """配置所属型号的第二种判法：在**来源页正文**里找与这份配置同行／同列的那个型号。

    只在配置文本自己没写型号时用。步骤：

    1. **锚点行**：配置文本里第一行能在页面正文里逐字找到的行——那一行就是这份配置在
       页面上的落点；
    2. **同行**：锚点行自己写着一个型号 → 用它（写法只差后缀时记候选自己的写法，
       见 :func:`_spelling_of_the_same_model`）；写着多个 → 只有**与候选自己是同一个**
       的那一个能被认（:func:`_one_model_of_the_candidate`），都不是就留空；
    3. **同列**：向上回溯几行，找最近一行写着型号的行（表头）。**按格序号对齐**：表头行
       与配置那一行的格数必须相等，配置的值**只落在一格**里，就取表头行同一格里的型号——
       那一格恰好一个型号才算（``|`` 分隔的表格列宽不定，用字符位置对齐会取错列）；
    4. **列定不下来时不取第一列**：配置的值在那一行的多个格里都出现（多列表格里几列的值
       完全一样，实测鑫贝西 ``BKQ-B75II`` 那张表四列都是 ``150℃``）、或格数对不上、或
       那一格不是恰好一个型号时，只有与候选同一个的那一个能被认；一个都对不上就留空；
    5. 都不成立 → ``(None, 原因)``。**不取正文里出现的第一个型号**：一页写五个型号的
       手册、一张四列的参数表，取错了就是把别人的参数挂到这个型号头上。
    """
    body = str(page_body or '')
    anchor_line = ''
    for line in str(configuration_text or '').splitlines():
        stripped = line.strip()
        if stripped and stripped in body:
            anchor_line = stripped
            break
    if not anchor_line:
        return None, '配置文本没有一行能在来源页正文里逐字定位，判不出配置所属型号'

    same_line = model_tokens_in_line(anchor_line, strict=True)
    if len(same_line) == 1:
        return _spelling_of_the_same_model(same_line[0], model), anchor_line
    if len(same_line) > 1:
        matched, detail = _one_model_of_the_candidate(same_line, model)
        if matched:
            return matched, anchor_line
        return None, detail

    index = body.find(anchor_line)
    row_start = body.rfind('\n', 0, index) + 1
    row_end = body.find('\n', index)
    anchor_row = body[row_start:row_end if row_end >= 0 else len(body)]
    anchor_cells = _split_cells(anchor_row)
    value_text = re.split(r'[|｜]', anchor_line)[-1].strip() or anchor_line
    # 值落在哪几格：多列表格里几列的值可以**一模一样**，这时格序号定不了列（旧代码取第一格，
    # 于是把表头第一列的型号挂到了候选头上）。
    value_cells = [position for position, (_, _, text) in enumerate(anchor_cells)
                   if value_text in text]
    if not value_cells:
        return None, '配置的值在来源页那一行里定位不到，判不出同列的型号'

    before = body[:row_start].splitlines()[-_CONFIGURATION_MODEL_LOOKBACK:]
    for line in reversed(before):
        header_tokens = model_tokens_in_line(line, strict=True)
        if not header_tokens:
            continue
        if len(header_tokens) == 1:
            return _spelling_of_the_same_model(header_tokens[0], model), line.strip()
        header_cells = _split_cells(line)
        if len(value_cells) != 1:
            column_reason = ('配置的值在来源页那一行的 %d 个格里都出现，定不了是哪一列'
                             % len(value_cells))
        elif len(header_cells) != len(anchor_cells):
            column_reason = ('表头行与配置那一行的格数对不上（%d 对 %d）'
                             % (len(header_cells), len(anchor_cells)))
        else:
            column_tokens = model_tokens_in_line(header_cells[value_cells[0]][2], strict=True)
            if len(column_tokens) == 1:
                return column_tokens[0], line.strip()
            if column_tokens:
                column_reason = ('这一份配置所在的那一格里写着多个型号（%s）'
                                 % '、'.join(column_tokens[:4]))
            else:
                column_reason = '这一份配置所在的那一格里没有型号'
        matched, detail = _one_model_of_the_candidate(header_tokens, model)
        if matched:
            return matched, line.strip()
        return None, ('表头行写着 %d 个型号（%s），但%s；%s'
                      % (len(header_tokens), '、'.join(header_tokens[:4]),
                         column_reason, detail))
    return None, '配置所在行的上面几行里没有写着型号，判不出配置所属型号'


def configuration_model_evidence(candidate, sources):
    """这条候选的配置属于哪个型号，以及依据。返回 ``(model, basis)``。

    ``basis`` 里一定有 ``url``（配置来源页）与 ``reason``（判不出时的原因）；
    判出来时还带上 ``rule``（靠什么认出来的）与 ``excerpt``（依据的那一行原文）。
    判不出返回空串型号——**不拿候选自己的型号顶上冒充**。
    """
    configuration = str(candidate.get('configuration') or '')
    if not configuration.strip():
        return '', {'reason': '这条候选没有配置，无从判断配置所属型号'}
    url = str(candidate.get('configuration_url') or '')
    page = next((source for source in (sources or [])
                 if isinstance(source, dict) and str(source.get('url') or '') == url), None)
    body = str((page or {}).get('body_text') or '')

    model, detail = configuration_model_from_configuration(configuration, candidate.get('model'))
    if model:
        return model, {'url': url, 'rule': 'configuration_text', 'excerpt': detail}
    if not body:
        return '', {'url': url,
                    'reason': '%s；配置来源页的正文不在这一轮的来源里' % detail}
    own_model = str(candidate.get('model') or '')
    # 这份配置是不是"多型号页里**只取目标型号那一份**"取出来的？是的话归属就是**查出来的**：
    # 取列／取行时列名（行首）与目标型号是同一性匹配（``_column_is_model``），不是猜的。
    # 重算一遍再与写出去的文本逐字比对——判据只有这一条，没有"大概像"。
    # 这是 ``设计规范`` §2c 的正面答案：EAB 224i 的配置现在只有它自己那一列，闸门要能
    # 说出"这份配置就是这台机器的"，而不是继续落在"判不出"。
    if own_model:
        from backend.services.product_configuration import model_scope_configuration
        scoped, kind, _verdict = model_scope_configuration(body, own_model)
        if scoped and scoped == str(configuration).strip():
            return own_model, {'url': url, 'rule': 'model_scope', 'scope': kind,
                               'excerpt': '配置是按「%s」从这一页的多型号对照表里取出的'
                                          '**目标型号那一份**（列名／行首与候选型号同一）' % kind}
    model, detail = configuration_model_from_page(body, configuration, candidate.get('model'))
    if model:
        return model, {'url': url, 'rule': 'page_request_row', 'excerpt': detail}
    # 判不出归属，而且这一页**并列着别的型号**（多型号对照表）：不能静默放行。
    # 判据不放宽（``configuration_model_conflict`` 对空串照旧不拦，"判不出就不猜"），
    # 但这件事必须让用户看到——"这几行里只有一列是这台机器的"。标注出口见
    # ``apply_configuration_model_gate``（给用户看的话进 gaps、给排查的话进 failures）。
    from backend.services.product_configuration import multi_model_siblings
    siblings = multi_model_siblings(body, own_model) if own_model else []
    if siblings:
        return '', {'url': url, 'rule': 'multi_model_page', 'multi_model': True,
                    'models': siblings[:CONFIGURATION_MODEL_GATE_NAMES], 'reason': detail}
    return '', {'url': url, 'reason': detail}


def record_configuration_model(candidate, sources):
    """把"这份配置属于哪个型号"如实记在候选上（设计规范）。

    为什么要有这个字段（``设计规范``）：洁净台那轮的 ``bekomat30`` 卡片，链接指向
    ``ProductDetail/634.html``（那页是 ``BBS-H1300``），配置却抽自 ``630.html``
    （那页是 ``BBS-V600``，配置第一行就写着），而 ``bekomat30`` 两个页面上都不存在。
    设计规范：**配置属于哪个型号就展示哪个型号**，不要再挂一个页面上不存在的型号名。

    这里只**如实记下**"这份配置属于谁"（``configuration_model`` ＋ 依据
    ``configuration_model_basis``，含依据那一行与来源 URL）。候选自己的 ``model``
    **一个字不改**：它是"查这个的价格"的键，``candidate_id`` 也按它算好写进卡片了。
    改成展示哪个名字是展示层（M5）的事。

    幂等：同一个候选调用两次，第二次原样返回；认不出时也留下 reason，供 gaps 汇总。
    """
    if not isinstance(candidate, dict) or not candidate.get('model'):
        return False
    if not str(candidate.get('configuration') or '').strip():
        return False
    if 'configuration_model' in candidate:
        return bool(candidate.get('configuration_model'))
    model, basis = configuration_model_evidence(candidate, sources)
    candidate['configuration_model'] = model
    candidate['configuration_model_basis'] = basis
    return bool(model)


def refetch_configuration_from_sources(run, candidate,
                                       page_limit=CONFIGURATION_REFETCH_PAGES_PER_CANDIDATE):
    """撤下去之前，把这一轮**已经抓到的**来源挨个试一遍。

    为什么要有这一步（``设计规范``）：洁净台那轮的贝茵 ``BCV-3S1`` 被判"没有配置"撤掉了，
    实测同一轮的来源里**至少三页**都能压出它的配置（``jinpanmed.com`` 219 字、
    ``beinglab.com`` 192 字、``medwant.com`` 98 字）——当时只试了错的那一页（成交公告，
    0 字）。这不是"抽不出"，是"没试完就被当成没有"。

    边界（一条都不许松）：

    * **不改判据**：``candidate_has_configuration`` 一个字没动，这里只是"判之前把该试的
      来源试完"；喂进 ``product_configuration`` 的还是同一套判据（与选页那条路同一把尺子）；
    * **不联网**：只看 ``run['sources']`` 里**已经抓到**的页面，不新增检索、不抓新页；
    * **不试无关的页**：正文里认不出这个候选型号的页面直接跳过（认型号用
      ``_is_candidate_family``，与选页那条路同一把尺子）；
    * 已经当过它的配置来源／详情页的那一页不重复试。

    返回 ``(spec 或 None, 试过的页面地址)``。
    """
    from backend.services.product_configuration import product_configuration
    from backend.services.research_planning import _is_candidate_family
    model = str(candidate.get('model') or '')
    brand = str(candidate.get('brand') or '')
    if not model:
        return None, []
    # 回捞只对"型号写法本身认得出型号"的候选做。真跑里有一条候选叫 ``ISO 5``——那是
    # **洁净等级**被发现阶段当成了型号，``模型`` 这一格里的 ``ISO 5`` 在页面上处处可见
    # （实测那页的配置里就写着「洁净等级：ISO 5 级」），拿它去回捞会给一个不存在的型号
    # 挂上别人的参数段。这一条比"正文含该候选型号"更**窄**，是收紧不是放宽。
    if not model_tokens_in_text(model):
        return None, []
    skip = {str(candidate.get('configuration_url') or ''),
            str((candidate.get('detail_page') or {}).get('url') or '')}
    # 被型号闸门拦下的那一页不再试：闸门的判断不能在"撤下前回捞"这一步被反过来推翻
    # （``gate_candidate_configuration`` 的 ② 已经把 ``configuration_url`` 清空，
    # 这里补上它拦下的那个地址）。
    skip |= {str(url) for url in [(candidate.get('configuration_model_gate') or {}).get('rejected_url')] if url}
    spellings = [spelling.casefold() for spelling in model_spellings(model) if spelling]
    attempted = []
    for source in run.get('sources') or []:
        if len(attempted) >= page_limit:
            break
        if not isinstance(source, dict) or source.get('body_status') != 'fetched':
            continue
        url = str(source.get('url') or '')
        body = source.get('body_text') or ''
        if not url or not body or url in skip:
            continue
        # 粗筛用纯字符串（页面可能几万字，逐行正则留给真正沾边的页面）。
        folded = body.casefold()
        if not any(spelling in folded for spelling in spellings):
            continue
        if not any(_is_candidate_family(line, model) for line in body.splitlines()):
            continue
        attempted.append(url)
        spec = product_configuration([source], model, brand)
        if spec:
            return spec, attempted
    return None, attempted


def has_named_parameter_row(text):
    """这段（重抽出来的）配置里有没有一行「参数名 | 值」或「参数名：值」。

    "有参数名"是**给用户看**的最低要求：只有值的那几行其实是一张表的几列值，表头行落在
    抽取窗口之外，用户看不懂它是什么参数。2026-10-07 实测 ``BWS-10``：重抽出来的 61 字是
    ``280×130×150 | 280×220×150`` / ``396×250×260 | 396×330×260`` / ``二孔 | 双列四孔``
    ——**一个参数名都没有**，这段文本按「未读到配置」处理。

    判据与 ``product_configuration`` 同一把尺子（``_looks_like_field`` 判名字形状、
    ``_VALUE_HEAD`` 判量值），只多三条"这一格其实是值"的排除：
    以数字／``[``／``(`` 开头、含 ``×``（尺寸分隔符）、是"数量词"形状。
    """
    from backend.services.product_configuration import (
        _VALUE_HEAD, _SYMBOL_VALUE, _grid_row_cells, _looks_like_field)
    for line in str(text or '').splitlines():
        cells = _grid_row_cells(line) or []
        if len(cells) >= 2:
            # 参数名可以**多级**（``温度 | 灭菌 | 105~135℃``：前两格都是行标签，``设计规范`` §7）。
            # 但中间格**是值**的那种不算——那是"一行多个型号 + 多列值"的整表行
            # （``量程 | 62g / 120g | 82g / 220g``），它没有"参数名 | 值"这个给用户看的形状。
            left, right = cells[0], cells[-1]
            if any(_VALUE_HEAD.match(cell) or _SYMBOL_VALUE.fullmatch(cell) or _MEASURE_CELL_MARK.search(cell)
                   for cell in cells[1:-1]):
                continue
        else:
            match = _NAME_VALUE_CELLS.match(str(line).strip())
            if not match:
                continue
            left, right = match.group(1).strip(), match.group(2).strip()
        if not left or not right:
            continue
        if not _looks_like_field(left):
            continue
        if _VALUE_HEAD.match(left) or _MEASURE_CELL_MARK.search(left) \
                or _QUANTITY_WORD_CELL.match(left):
            continue
        return True
    return False


def configuration_model_target(run, model):
    """本轮候选里有没有这个型号（三份名单都算）：返回 ``(候选, 名单名)`` 或 ``(None, '')``。

    ``alternatives`` / ``reference_candidates`` / ``discovered_candidates`` 都算——设计规范：「那不是应该把 BBS-V600 列入候选吗」。三份名单里的候选**字典对象是共用的**，
    所以交给哪一份名单里的那一条，卡片上都看得到。
    """
    key = identity(model)
    if not key:
        return None, ''
    for roster in ('alternatives', 'reference_candidates', 'discovered_candidates'):
        for candidate in run.get(roster) or []:
            if isinstance(candidate, dict) and identity(candidate.get('model')) == key:
                return candidate, roster
    return None, ''


def brand_from_configuration_page(run, url, model):
    """配置页上写着的品牌（读不到就留空，不猜）。返回 ``(brand, 依据那一行)``。

    与 ``research_planning`` 的 ``text_scan`` 那条路同一个口径：品牌**只从页面上写着这个
    型号的那一行里读**（``brand_from_source_line``），读不到就留空，不从别处借、不按常识补。
    """
    page = next((source for source in run.get('sources') or []
                 if isinstance(source, dict) and str(source.get('url') or '') == url), None)
    body = str((page or {}).get('body_text') or '')
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or not contains_model(stripped, model):
            continue
        brand = brand_from_source_line(stripped, model=model)
        if brand:
            return brand, stripped
    return '', ''


def new_candidate_from_configuration(run, source_candidate, model, held):
    """由配置页认出的型号 → 新建一条候选。

    业务口径：「那不是应该把 BBS-V600 列入候选吗」。这一条是**派生**的——
    不是发现阶段找到的，是"某条候选拿错了别家的配置"这一步把它带出来的，所以：

    * ``source`` 与 ``reference_reason`` 都写 ``configuration_page_model``，
      ``reference_note`` 一句话说明来路；
    * **不进核验名额**：名额与 ``slotted`` 早在上游定稿，这里只往 ``reference_candidates``
      里落一张卡（``place_slotted_candidates`` 也不会把它当"进过名额却没落点"补进去）；
    * 品牌只从配置页正文里读，读不到留空；
    * ``candidate_id`` 照常补（id 与它在不在核验名额里无关，缺了卡上的查价按钮会 404）。
    """
    url = str(held.get('configuration_url') or '')
    brand, excerpt = brand_from_configuration_page(run, url, model)
    candidate = {
        'brand': brand,
        'model': model,
        'configuration': str(held.get('configuration') or ''),
        'configuration_source': str(held.get('configuration_source') or ''),
        'configuration_url': url,
        'configuration_model': model,
        'configuration_model_basis': held.get('configuration_model_basis') or {},
        'url': url,
        'excerpt': excerpt,
        'source': 'configuration_page_model',
        'reference_reason': 'configuration_page_model',
        'reference_note': ('这一条是按配置页上写着的型号补出来的：这一份配置原来挂在'
                           '本轮候选「%s」上，而那一页讲的是它。配置与出处都按原文照抄，'
                           '没有为它另外检索。' % (source_candidate.get('model') or '')),
    }
    record_candidate_source_pages(candidate)
    taken = set()
    for roster in ('alternatives', 'reference_candidates', 'discovered_candidates'):
        for other in run.get(roster) or []:
            if isinstance(other, dict) and other.get('candidate_id'):
                taken.add(str(other['candidate_id']))
    assign_candidate_ids([candidate], taken)
    return candidate


def attribute_configuration(run, source_candidate, model, held):
    """把候选拿错的那一份配置**归给它真正的型号**（设计规范）。

    两档，顺序固定：

    * 本轮候选清单里已经有这个型号 → **归给它**；它本来就有配置时**一个字不改**
      （同一份配置归给本来就该有它的那条候选，等价于去重）；
    * 清单里没有 → 用这个型号**新建一条候选**（见 ``new_candidate_from_configuration``）。

    返回 ``(attribution, target)``：``'handed'``／``'already'``／``'created'``。
    """
    target, _roster = configuration_model_target(run, model)
    if target is not None:
        if str(target.get('configuration') or '').strip():
            return 'already', target
        target['configuration'] = str(held.get('configuration') or '')
        target['configuration_source'] = str(held.get('configuration_source') or '')
        target['configuration_url'] = str(held.get('configuration_url') or '')
        target['configuration_model'] = model
        target['configuration_model_basis'] = held.get('configuration_model_basis') or {}
        target['configuration_attributed_from'] = str(source_candidate.get('model') or '')
        return 'handed', target
    created = new_candidate_from_configuration(run, source_candidate, model, held)
    created['configuration_attributed_from'] = str(source_candidate.get('model') or '')
    run.setdefault('reference_candidates', []).append(created)
    return 'created', created


def _configuration_gate_trace(candidate, rule, scope_undetermined=False):
    """把**这一轮**闸门的结论如实写在候选上（覆盖上一轮的留痕）。

    ``设计规范`` §2b 缺陷 2：``ok`` 分支原来直接 return，一个字都不覆盖，于是候选被
    ``withdraw_unconfigured_candidates`` 里的回捞修好、二次进闸门拿到 ``action='ok'`` 之后，
    卡片与排查数据上还留着上一轮的 ``action='dropped' / attribution='created'``——显示
    "这份配置已经转走"，而它正拿着自己的配置。留痕字段必须与当前状态一致。

    上一轮的 ``rejected_url`` 等**事实**保留（``refetch_configuration_from_sources`` 靠它
    不再去试那个已被判成别的型号的页），上一轮的 ``action`` 降格成 ``previous_action``
    留作线索——覆盖的是结论，不是把发生过的转移抹掉。
    """
    previous = candidate.get('configuration_model_gate') or {}
    trace = {
        'candidate_model': str(candidate.get('model') or ''),
        'action': 'ok',
        'attribution': '',
        'attributed_to': '',
        'attributed_candidate_id': '',
        'rule': rule,
    }
    for key in ('rejected_url', 'rejected_model', 'rejected_chars', 'tried_urls',
                'relocation_rejected', 'used_url'):
        if previous.get(key):
            trace[key] = previous[key]
    if previous.get('action'):
        trace['previous_action'] = str(previous['action'])
    if scope_undetermined:
        # 这一份配置来自多型号对照表、而它属于哪一行判不出来（判据没放宽，照旧采用）。
        # 汇总出口在 ``apply_configuration_model_gate``：人话进 gaps、逐条进 failures。
        trace['scope_undetermined'] = True
    candidate['configuration_model_gate'] = trace
    return trace


def gate_candidate_configuration(run, candidate):
    """采用一份配置之前的那道闸门：它所属的型号必须与候选自己的型号同一个。

    不一致时（``configuration_model`` = X，候选自己的型号 = Y）按顺序两档
    （2026-10-07 升级：从"失去配置就撤"改成"配置归属转移"）：

    ① ``source_pages`` 里有 ``origin='discovered'`` 的页面、且它抽得出**合格**的配置
      （过产出质量闸与型号闸）→ Y 改用那一份；
    ② 无论①走没走成，**Y 原来拿错的那一份配置都按归属转移还给 X**：本轮候选里已经有 X
      就归给它（它本来就有配置时一个字不改），没有就用 X 的身份新建一条候选。
      ①走不成时 Y 视为「没读到配置」，交给既有撤除流程
      （``candidate_has_configuration`` 一个字没放宽）。

    **没有配置就没有可转移的东西**（``设计规范`` §2b 缺陷 1）：候选 ``configuration`` 为空
    时，残留的 ``configuration_model``（配置被清空后留下的字段）先清掉，闸门直接判 ok，
    既不建 0 字候选，也不写"配置与出处都按原文照抄"那句话。

    返回 ``{'action': 'ok'|'relocated'|'dropped', 'attribution': ''|'handed'|'already'|'created',
    'target': 目标候选或 None}``。这一轮"配置来自多型号对照表、归属判不出"的事实用
    ``configuration_model_gate['scope_undetermined']`` 如实留下，由
    ``apply_configuration_model_gate`` 汇聚成 gaps 与 failures——判据一个字没放宽。
    """
    # 认定前移（就地重算一次）：闸门要读的 ``configuration_model`` 是 迭代规范 那个字段，
    # 正常流程里它在 ``record_configuration_model`` 那一步已经算好；这里再调一次是
    # **幂等**的（算过就原样返回），只保证"配置在、型号还没算过"时不会静默跳过。
    record_configuration_model(candidate, run.get('sources'))
    if not str(candidate.get('configuration') or '').strip():
        candidate.pop('configuration_model', None)
        candidate.pop('configuration_model_basis', None)
        _configuration_gate_trace(
            candidate, rule='这一条候选没有配置：没有可转移的东西，闸门不动名单')
        return {'action': 'ok', 'attribution': '', 'target': None}
    if not configuration_model_conflict(candidate):
        _configuration_gate_trace(
            candidate, rule='配置所属型号与候选型号一致（或判不出）：这一份配置照常采用',
            scope_undetermined=bool(
                (candidate.get('configuration_model_basis') or {}).get('multi_model')))
        return {'action': 'ok', 'attribution': '', 'target': None}
    model = str(candidate.get('model') or '')
    found = str(candidate.get('configuration_model') or '')
    held = {
        'configuration': str(candidate.get('configuration') or ''),
        'configuration_source': str(candidate.get('configuration_source') or ''),
        'configuration_url': str(candidate.get('configuration_url') or ''),
        'configuration_model_basis': candidate.get('configuration_model_basis') or {},
        'chars': len(str(candidate.get('configuration') or '')),
    }
    spec, tried, rejected = configuration_from_discovered_page(run, candidate)
    gate = {
        'candidate_model': model,
        'rejected_model': found,
        'rejected_url': held['configuration_url'],
        'rejected_chars': held['chars'],
        'tried_urls': tried,
        'relocation_rejected': rejected,
    }
    if spec:
        candidate['configuration'] = spec['text']
        candidate['configuration_source'] = spec['source']
        candidate['configuration_url'] = spec['url']
        candidate.pop('configuration_model', None)
        candidate.pop('configuration_model_basis', None)
        record_configuration_model(candidate, run.get('sources'))
        gate['action'] = 'relocated'
        gate['used_url'] = str(spec['url'] or '')
        gate['rule'] = '配置所属型号与候选型号不一致：改用发现这一条候选的那一页上的配置'
    else:
        gate['action'] = 'dropped'
        gate['rule'] = ('配置所属型号与候选型号不一致，也没有换到写着它自己型号的那一页：'
                        '这一份配置没有采用，按「没读到配置」处理')
        candidate['configuration'] = ''
        candidate['configuration_source'] = ''
        candidate['configuration_url'] = ''
        candidate.pop('configuration_model', None)
        candidate.pop('configuration_model_basis', None)
        # 给排查用的话（`failures`：带字段名与原文，不渲染到页面）与给人看的话
        # （`gaps`，见 ``apply_configuration_model_gate``）分开写（``设计规范`` §5）。
        run.setdefault('failures', []).append({
            'stage': '候选配置型号闸门',
            'type': 'ConfigurationModelMismatch',
            'code': 'candidate_configuration_model_mismatch',
            'location': '候选 %s ← %s' % (model, held['configuration_url']),
            'error': ('配置所属型号 %s 与候选型号 %s 不一致（依据 %s）：这一份配置没有挂在它名下，'
                      '已按归属转移还给 %s' % (found, model, held['configuration_url'], found)),
        })
    attribution, target = attribute_configuration(run, candidate, found, held)
    gate['attribution'] = attribution
    gate['attributed_to'] = str((target or {}).get('model') or '')
    gate['attributed_candidate_id'] = str((target or {}).get('candidate_id') or '')
    candidate['configuration_model_gate'] = gate
    return {'action': gate['action'], 'attribution': attribution, 'target': target}


def apply_configuration_model_gate(run, candidates):
    """把型号闸门跑在一批候选上，并把**给人看的那句话**写进 ``gaps``。

    返回 ``{'relocated', 'dropped', 'handed', 'created'}`` 四份清单：``handed`` 是
    ``(来源候选, 目标候选)``（配置归给了本轮已有的那条），``created`` 是新建出来的候选。
    逐条清单不印进 ``gaps``（那是给排查的），只印人话。
    """
    result = {'relocated': [], 'dropped': [], 'handed': [], 'created': []}
    # 「判不出归属」与「取不出就留空」这两批不进返回值：``result`` 的四份清单是既有契约，
    # 多一个键就会换掉调用方读到的形状。它们只在下面汇总成 gaps 与 failures 两处出口。
    scope_undetermined = []
    scope_blank = []
    created_ids, seen = set(), set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict) or not candidate.get('model'):
            continue
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        outcome = gate_candidate_configuration(run, candidate)
        if outcome['action'] == 'relocated':
            result['relocated'].append(candidate)
        elif outcome['action'] == 'dropped':
            result['dropped'].append(candidate)
        if outcome['attribution'] == 'handed':
            result['handed'].append((candidate, outcome['target']))
        elif outcome['attribution'] == 'created' and id(outcome['target']) not in created_ids:
            created_ids.add(id(outcome['target']))
            result['created'].append(outcome['target'])
        if (candidate.get('configuration_model_gate') or {}).get('scope_undetermined'):
            scope_undetermined.append(candidate)
        if not str(candidate.get('configuration') or '').strip():
            found = configuration_scope_undetermined(run, candidate)
            if found:
                candidate['configuration_scope_gap'] = {
                    'url': found[0], 'verdict': found[1],
                    'rule': '这一页是多型号对照表、或者表里没有这台机器那一列：按「取不出就留空」处理'}
                scope_blank.append((candidate, found[0], found[1]))

    def names(rows):
        return '、'.join(str(candidate.get('model') or '') for candidate in rows[:CONFIGURATION_MODEL_GATE_NAMES])

    if result['relocated']:
        prefix = '配置页型号闸门：'
        if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
            run.setdefault('gaps', []).append(
                '%s有 %d 条候选接上的参数页讲的是**别的型号**（配置所属型号与候选自己的型号对不上），'
                '已改用发现这一条候选的那一页上的配置：%s。逐条的旧页与新页记在候选自己的 '
                'configuration_model_gate 上。' % (prefix, len(result['relocated']), names(result['relocated'])))
    if result['dropped']:
        prefix = '配置页型号闸门（配置归属转移）：'
        if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
            run.setdefault('gaps', []).append(
                '%s有 %d 条候选接上的参数页讲的是别的型号（%s）：**这一份配置已经归给它真正的型号**'
                '——其中 %d 条归到本轮已有的那条候选上、%d 条按配置页认出的型号新建了候选；'
                '原来挂着它的候选按「没读到配置」处理（撤下之前仍会先把这一轮已经抓到的来源试完）。'
                '逐条记在候选自己的 configuration_model_gate 上。'
                % (prefix, len(result['dropped']), names(result['dropped']),
                   len(result['handed']), len(result['created'])))
    if result['created']:
        prefix = '配置页型号闸门（新建候选）：'
        if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
            run.setdefault('gaps', []).append(
                '%s有 %d 条候选是由配置页上写着的型号补出来的（%s）：它们没有参与本轮检索、'
                '也没有占核验名额，配置与出处都按原文照抄，请点开原文确认。'
                % (prefix, len(result['created']), names(result['created'])))
    if scope_undetermined:
        # 判不出归属时**不许静默放行**（``设计规范`` §2b／§6 第 4 条）：判据一个字没放宽
        # （``configuration_model_conflict`` 对空串照旧不拦），但这件事要说出**给用户看的话**
        # ——卡片上的配置原文可能含着别的型号的数值，用户得知道"这几行里只有一列是这台机器的"。
        # 逐条事实进 ``failures``（给排查用，带型号清单与来源页），两处分开写（``设计规范`` §5）。
        prefix = '配置页多型号对照（归属未定）：'
        if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
            siblings = []
            for candidate in scope_undetermined:
                for model in (candidate.get('configuration_model_basis') or {}).get('models') or []:
                    if model not in siblings:
                        siblings.append(model)
            run.setdefault('gaps', []).append(
                '%s有 %d 条候选接上的参数页是**多型号对照表**（页面上并列写着 %s 等型号），'
                '而这几行里哪一行属于这台机器**判不出来**：卡片上的配置按原文照抄，'
                '请点开原文按型号核对。逐条记在候选自己的 configuration_model_basis 上。'
                % (prefix, len(scope_undetermined),
                   '、'.join(siblings[:CONFIGURATION_MODEL_GATE_NAMES]) or '多个型号'))
        for candidate in scope_undetermined:
            basis = candidate.get('configuration_model_basis') or {}
            run.setdefault('failures', []).append({
                'stage': '候选配置型号闸门',
                'type': 'ConfigurationModelUndetermined',
                'code': 'candidate_configuration_model_undetermined',
                'location': '候选 %s ← %s' % (candidate.get('model') or '',
                                             basis.get('url') or candidate.get('configuration_url') or ''),
                'error': ('配置来源页是多型号对照表（页面上并列写着 %s），这些行属于哪个型号判不出来：'
                          '按「判不出就不猜」放行，配置原文照抄。依据：%s'
                          % ('、'.join(basis.get('models') or []) or '（未列出）',
                             basis.get('reason') or '配置所在行的上面几行里没有写着型号')),
            })
    if scope_blank:
        # "取不出就留空"这件事原先只在 ``product_configuration`` 的 ``logger.debug`` 里，
        # 用户看到的是"这台机器没配置"，真相是"没认出那张表"（``设计规范`` §7）。
        # 这里接到既有的两个出口：人话进 ``gaps``、逐条（页面地址＋判据）进 ``failures``。
        prefix = '配置页没认出那张表：'
        if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
            run.setdefault('gaps', []).append(
                '%s有 %d 条候选（%s）接上的参数页是**多型号对照表**、或者表里根本没有这台机器那一列：'
                '按「取不出就不取」处理——宁可不给配置，也不把整张表当成它的参数。'
                '逐条的页面地址与判据记在候选自己的 configuration_scope_gap 上。'
                % (prefix, len(scope_blank), names([item[0] for item in scope_blank])))
        for candidate, url, verdict in scope_blank:
            run.setdefault('failures', []).append({
                'stage': '候选配置型号闸门',
                'type': 'ConfigurationTableNotRecognized',
                'code': 'candidate_configuration_table_not_recognized',
                'location': '候选 %s ← %s' % (candidate.get('model') or '', url),
                'error': ('这一页认不出属于 %s 的那一列（按列通路的判定是 %s）：'
                          '按「取不出就留空」处理，没有退回整表压缩' % (candidate.get('model') or '', verdict)),
            })
    return result


def configuration_model_conflict(candidate):
    """这份配置所属的型号与候选自己的型号**是不是同一个**（判不出＝不算不一致）。

    判据只有一条：迭代规范 已经算好的 ``configuration_model`` 与 ``model`` 归一后不同
    （``identity``，与配对型号同一把尺子）。空串一律不拦——``设计规范`` 的口径是
    "判不出就不猜"，判不出不等于不一致。
    """
    if not isinstance(candidate, dict):
        return False
    found = str(candidate.get('configuration_model') or '').strip()
    model = str(candidate.get('model') or '').strip()
    if not found or not model:
        return False
    return identity(found) != identity(model)


def configuration_scope_undetermined(run, candidate):
    """这一条"没有配置"的候选，是不是因为**页面上那张表取不出它那一列**。

    返回 ``(url, verdict)`` 或 ``None``。只看这一条候选自己的页面（``detail_page`` 与
    ``source_pages`` 里**已经抓到**的页），不联网、不新增来源；判据直接用抽配置那条
    按列通路的原话（``product_configuration.model_scope_configuration`` 的 ``verdict``），
    所以这里说的"没认出那张表"与"为什么没抽到配置"是**同一把尺子**，不是另写一套。

    为什么要有它（``设计规范`` §7）：留空原先只在 ``product_configuration`` 的
    ``logger.debug`` 里，用户看到的是"这台机器没配置"，真相是"没认出那张表"——
    "跳过"与"丢数据"没有分开（``设计规范`` §3.4）。
    """
    from backend.services.product_configuration import model_scope_configuration
    model = str(candidate.get('model') or '')
    if not model:
        return None
    by_url = {}
    for source in run.get('sources') or []:
        if isinstance(source, dict):
            by_url[str(source.get('url') or '')] = source
    urls = []
    detail = str((candidate.get('detail_page') or {}).get('url') or '')
    if detail:
        urls.append(detail)
    for row in candidate.get('source_pages') or []:
        if isinstance(row, dict) and row.get('url'):
            urls.append(str(row['url']))
    tried = 0
    for url in urls:
        source = by_url.get(url)
        if not source or source.get('body_status') != 'fetched':
            continue
        body = source.get('body_text')
        if not isinstance(body, str) or model not in body:
            continue
        tried += 1
        _text, _kind, verdict = model_scope_configuration(body, model)
        if verdict == 'leave-empty':
            return url, verdict
        if tried >= CONFIGURATION_SCOPE_PAGES:
            break
    return None


def configuration_from_discovered_page(run, candidate):
    """① 换页：改用候选 ``source_pages`` 里 ``origin='discovered'`` 那一页抽配置。

    为什么是这一档（2026-10-07 真跑实测）：``BXM-145V-MII``（博迅）被接上的参数页是
    ``boxun.com.cn/product-402.html``，那一页讲的是同系列的 ``BXM-170V-MⅡ``（正文里
    "型号：BXM-170V-MⅡ"出现 3 次，候选型号只在页脚"相关产品"里出现过 1 次）——选页
    那一步只检查"这一页全文里出现过候选型号"，被页脚骗过。真正讲这一台的那一页
    （``product-401.html``）当轮**已经抓到了**，就在候选自己的 ``source_pages`` 里。

    边界：

    * 只认 ``origin='discovered'``（发现这条候选的那一页），别的来路的页不拿来顶；
    * 抽取用 ``product_configuration``——与选页那条路**同一把尺子**，不为这一档放宽；
    * 抽出来的那一份**还要过两道闸**：①**产出质量闸**（``has_named_parameter_row``：
      至少要有一行「参数名 | 值」，只有值的那一段不算配置）；②型号闸门（判得出型号
      且不是候选自己的型号就不采用）。两道都过不了就换下一页。

    返回 ``(spec 或 None, 试过的地址, 被两道闸拦下的)``；第三个是
    ``[{'url', 'chars', 'reason'}]``，给留痕用（"跳过 ≠ 丢数据"）。
    """
    from backend.services.product_configuration import product_configuration
    if not isinstance(candidate, dict) or not candidate.get('model'):
        return None, [], []
    model = str(candidate.get('model') or '')
    brand = str(candidate.get('brand') or '')
    by_url = {}
    for source in run.get('sources') or []:
        if isinstance(source, dict):
            by_url[str(source.get('url') or '')] = source
    tried, rejected = [], []
    for row in candidate.get('source_pages') or []:
        if not isinstance(row, dict) or str(row.get('origin') or '') != 'discovered':
            continue
        url = str(row.get('url') or '').strip()
        source = by_url.get(url)
        if not url or not source or source.get('body_status') != 'fetched':
            continue
        tried.append(url)
        spec = product_configuration([source], model, brand)
        if not spec:
            continue
        # ① 产出质量闸：只有值、没有参数名的重抽结果不算配置（实测 BWS-10 那 61 字）。
        if not facade_attr('has_named_parameter_row', has_named_parameter_row)(spec['text']):
            rejected.append({'url': url, 'chars': len(str(spec['text'] or '')),
                             'reason': '重抽出来的配置里没有参数名行（只剩值），按「未读到配置」处理'})
            continue
        # ② 型号闸门：这一页抽出来的也得是这个型号的配置。
        found, _basis = configuration_model_evidence(
            {'model': model, 'configuration': spec['text'], 'configuration_url': spec['url']},
            run.get('sources'))
        if found and identity(found) != identity(model):
            rejected.append({'url': url, 'chars': len(str(spec['text'] or '')),
                             'reason': '这一页抽出来的配置属于别的型号（%s）' % found})
            continue
        return spec, tried, rejected
    return None, tried, rejected


def candidate_is_verified(candidate):
    """这条候选"有没有核验过"——与**卡片上那行事实标记**同一口径，不新造第二套语义。

    页面那行是 ``candidateFacts``（``frontend/src/services/candidateRoster.js``）算的：
    「已查价 · 已逐项比对 N 项」。这里逐条对着它来：

    * 逐项对齐读到过项（``config_match.items`` 非空）；
    * 或者查过价——有价格依据（``prices`` 非空），或者 ``price_lookup.status`` 不是
      "没查"的那两种字面（``not_requested`` / ``not_attempted``，与前端那份常量
      ``NOT_REQUESTED`` / ``NOT_ATTEMPTED`` 同一组值）。

    注意不能写成 ``bool(candidate.get('config_match'))``：空字典 ``{}`` 也是假值，而
    真跑里几乎每条候选都带着一个 ``price_lookup: {'status': 'not_attempted'}``——
    那个"有字段"不等于"核验过"，按前端口径它算没查。
    """
    if not isinstance(candidate, dict):
        return False
    match = candidate.get('config_match')
    items = match.get('items') if isinstance(match, dict) else None
    if isinstance(items, list) and items:
        return True
    if isinstance(candidate.get('prices'), list) and candidate['prices']:
        return True
    lookup = candidate.get('price_lookup')
    status = str((lookup or {}).get('status') or '').strip() if isinstance(lookup, dict) else ''
    return status not in ('', NOT_REQUESTED, NOT_ATTEMPTED)


def merge_candidate_rosters(run):
    """把两份候选名单合成一份 ``run['candidates']``，并返回它。

    设计规范：「现在已经不分正式候选和参考候选了，只要在候选区就行，后端代码也别再
    区分了」。这一步只做**输出**：``alternatives`` 在前、``reference_candidates`` 在后
    （与页面今天的顺序逐字一致），每条带一个 ``verified`` 标记（见
    :func:`candidate_is_verified`，字段值就是卡片上那行事实标记的同一个口径）。

    两个旧数组一个字不动：它们还挂着价格查价、核验名额、撤除、型号闸门、知识库沉淀、
    历史台账与几十个测试。合并清单是**浅拷贝**（``dict(candidate, verified=...)``），
    所以旧数组里的条目连字段都不会多一个；展示层完全切过来之后再做第二步。
    """
    merged = []
    for candidate in list(run.get('alternatives') or []) + list(run.get('reference_candidates') or []):
        if not isinstance(candidate, dict):
            continue
        merged.append(dict(candidate, verified=candidate_is_verified(candidate)))
    run['candidates'] = merged
    return merged


def withdraw_unconfigured_candidates(run):
    """Take candidates with no configuration off **both** visible rosters.

    Two owner decisions, one behaviour:

    * 2026-10-06 (``02-判定暴露的问题.md`` §Q3a): "找不到配置的就不要展示给用户了",
      and when asked whether to demote those candidates to the collapsible lead list
      instead, the answer was 「撤掉吧」. That is why this never *moves* a candidate
      from ``alternatives`` to ``reference_candidates`` -- a folded card is still a
      card.
    * 2026-10-07: the same rule applies to ``reference_candidates`` itself. A lead
      with no configuration is the same empty configuration block on the same kind
      of card, so it is withdrawn too. Cards that *do* carry a configuration stay
      exactly as they were; the roster is narrowed, never deleted.

    Measured on the 58 stored ledgers, both readings of the same rule:

    * raw ledger counts (``candidate_has_configuration`` over the stored objects):
      ``alternatives`` 139 -> 49 (90 without a configuration, 65%),
      ``reference_candidates`` 628 -> 267 (361 without one, 57%, spread over 50 runs);
    * in the M4 replay's own bookkeeping (``output/e2e-final/m4-replay-result.json``):
      ``alternatives`` 138 -> 48, ``reference_candidates`` 616 -> 268.

    ``discovered_candidates`` is left alone on purpose: what is withdrawn is the
    card shown to the user, not the fact that discovery saw the model. The
    ``price_lookup`` note stays on the discovered list, and one sentence in ``gaps``
    says how many were withdrawn and why -- "skipped" and "lost" must stay
    distinguishable (``设计规范`` §3.4/§5). The sentence is written once per run
    even though this is called for two rosters, and calling it twice in one run
    does not append it twice.

    A third decision (2026-10-07) added one step **before** the withdrawal itself: a
    candidate judged "no configuration" first gets this round's already-captured
    sources retried (see :func:`refetch_configuration_from_sources`). The 洁净台
    ``BCV-3S1`` case was a pure false negative -- three pages already in the round
    yielded 219/192/98 characters of its configuration, and the only page tried was
    the award notice that yields nothing. Only candidates *about to be withdrawn* are
    retried, nothing is fetched from the network, and the judge
    (``candidate_has_configuration``) is unchanged: this is "try what you already
    have before saying you have nothing", not a looser rule. Candidates that still
    have nothing say so in ``gaps`` (which pages were tried) and carry the attempted
    URLs on ``configuration_refetch``.

    Returns the withdrawn candidates, ``alternatives`` first.
    """
    withdrawn = []
    refetched = []
    tried_empty = []          # 撤下前试过已有来源、仍然没读到的候选
    tried_pages = 0
    budget = CONFIGURATION_REFETCH_CANDIDATES_PER_RUN
    for key in ('alternatives', 'reference_candidates'):
        kept = []
        for candidate in run.get(key) or []:
            if not (isinstance(candidate, dict) and candidate.get('model')) \
                    or candidate_has_configuration(candidate):
                kept.append(candidate)
                continue
            # 判为"没有配置"之后、真正撤下之前：把这一轮**已经抓到的**来源试完。
            # 判据一个字没放宽，这一步只是别再"没试完就当成没有"（设计规范）。
            if budget > 0:
                budget -= 1
                spec, attempted = refetch_configuration_from_sources(run, candidate)
                candidate['configuration_refetch'] = {
                    'attempted': attempted,
                    'found_url': (spec or {}).get('url', ''),
                    'rule': '撤下前回捞本轮已有来源'}
                if spec:
                    candidate['configuration'] = spec['text']
                    candidate['configuration_source'] = spec['source']
                    candidate['configuration_url'] = spec['url']
                    record_configuration_model(candidate, run.get('sources'))
                    # 型号闸门（采用一份配置之前的那一道）：回捞到的这一份如果也不属于
                    # 候选自己的型号，就按"没读到配置"处理——别把闸门刚拦下的东西从
                    # 后门放回来。拦下时留痕仍在 ``configuration_refetch`` 与
                    # ``configuration_model_gate`` 上，然后继续往下走撤除。
                    if gate_candidate_configuration(run, candidate)['action'] != 'dropped':
                        refetched.append(candidate)
                        kept.append(candidate)
                        continue
                tried_pages += len(attempted)
                if attempted:
                    tried_empty.append(candidate)
            withdrawn.append(candidate)
        run[key] = kept
    if refetched and not any(str(gap).startswith('撤下之前')
                             for gap in run.get('gaps') or []):
        names = [str(candidate.get('model') or '') for candidate in refetched][:6]
        run.setdefault('gaps', []).append(
            '撤下之前把这一轮已经抓到的来源试完了：%d 条候选（%s）在已有来源里补到了配置，'
            '没有撤下；出处记在候选自己的 configuration_url 上。'
            % (len(refetched), '、'.join(names)))
    if not withdrawn:
        return []
    prefix = '按「找不到配置就不展示给用户」'
    if not any(str(gap).startswith(prefix) for gap in run.get('gaps') or []):
        names = [str(candidate.get('model') or '') for candidate in withdrawn][:6]
        tried = ''
        if tried_empty:
            hosts = []
            for candidate in tried_empty:
                for url in (candidate.get('configuration_refetch') or {}).get('attempted') or []:
                    host = host_of(url)
                    if host and host not in hosts:
                        hosts.append(host)
            tried = ('撤下前已经把这一轮已有的来源试完了：其中 %d 条候选试过 %d 页'
                     '（%s），都没有读到配置；试过的每一条地址记在该候选的 '
                     'configuration_refetch 上。' % (len(tried_empty), tried_pages,
                                                    '、'.join(hosts[:6])))
        else:
            tried = '撤下前在已有来源里没有可试的页面。'
        run.setdefault('gaps', []).append(
            '%s撤下了 %d 条候选（%s）：这一轮没有读到它们的任何配置，%s'
            '卡片上的配置栏只能是空的；它们仍留在发现记录里，没有被删除。'
            % (prefix, len(withdrawn), '、'.join(names), tried))
    return withdrawn
