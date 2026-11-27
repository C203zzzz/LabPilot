"""Extracted procurement research helpers (report_helpers)."""
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
from backend.services.research.constants import DOCUMENT_HEAD_CHARS, EMPTY_RESULT_ALERT_STREAK, EMPTY_SECTION_NOTES, ENTRY_TEXT_KEYS, FAILURE_KIND_LABELS, GAP_LABELS, GAP_TOKENS, INTAKE_CARRIED_FIELDS, NOISE_DOCUMENT_MARKERS, NOT_PROVIDED_HINT, PRICE_STATE_LABELS, REFERENCE_REASONS, SALES_FIELDS, SUPPLIER_FIELDS, TRANSSHIPMENT_WORDS, _CHANNEL_GAP_PREFIXES, _ONE_TIME_TOKEN, _URL_QUERY, blank
from backend.services.research.price_helpers import _date_range, award_records, has_award_record, quoted_model_note, reconcile_tender_body_gaps


def uncovered_intake_fields(state, payload):
    """Conditions the interview recorded that this baseline payload would drop."""
    from backend.services.procurement_intake import draft_baseline
    draft = state.get('draft') if isinstance(state, dict) else None
    if not isinstance(draft, dict):
        draft = draft_baseline(state) if isinstance(state, dict) else {}
    return [key for key in INTAKE_CARRIED_FIELDS
            if not blank(draft.get(key)) and blank(payload.get(key))]


def sanitize_gap_text(gap):
    """缺口文字给读者看的那一份：内部代号翻成人话，异常类名去掉。

    只处理**展示副本**。``run['gaps']`` 落库的那一份一个字都不动：它是这一轮检索的
    原始记账，排查的人要靠 ``tender_body_unverified``、``TimeoutError`` 认出发生了什么。
    翻译表在 ``report_library`` 一处（导出的 Markdown 用的是同一张表）。
    """
    from backend.services.report_library import humanize_gap_text
    return humanize_gap_text(gap)


def _clip(text, limit=60):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


def answer_field(question):
    """从追问原文里读回它问的是哪个字段（``…（字段 supplier_price）``）。

    盲测那一轮的销售答复就是按这个约定写的；读得回来，"问了、说没有"才能落到
    具体字段上，而不是变成一句放在哪都对不上的自由文本。
    """
    for match in re.finditer(r'字段\s*([a-z][a-z_]*)|[（(]\s*([a-z][a-z_]*)\s*[）)]', str(question or '')):
        name = match.group(1) or match.group(2)
        if name in SUPPLIER_FIELDS:
            return name
    return ''


def missing_information_state(run, field, label=''):
    """这一项为什么是空的：没问，还是问了、对方说没有。

    采购员要能分清这两件事——"没问"意味着还得继续追，"问了说没有"意味着这条路
    到头了。所以报告里每一个空着的销售字段都要能回答这个问题（盲测 B01 第 8c 条：
    5 项销售明确答过「资料中未提供」，报告里只写「用户未填写」）。
    """
    revision = run.get('supplier_revision') or {}
    if field in {str(name) for name in (revision.get('not_provided') or [])}:
        return '销售已回复：资料中未提供'
    for answer in revision.get('answers') or []:
        if not isinstance(answer, dict):
            continue
        text = str(answer.get('answer') or '')
        named = answer.get('field') or answer_field(answer.get('question'))
        if not text or named != field or not NOT_PROVIDED_HINT.search(text):
            continue
        return '销售已回复：资料中未提供（原话：%s）' % _clip(text)
    if revision:
        # 这一轮答复里根本没提这一项：既不能说"没问"（可能前面几轮问过），也不能说
        # "问了说没有"（那是编造）。只写这次回复的实际范围。
        return '本轮销售回复没有涉及这一项（既没有给值，也没有说明资料里有没有）'
    return '本轮没有向销售询问这一项'


def note_provider_result(run, provider_name, *, status, results):
    """Track consecutive ok-but-empty calls; report each threshold crossing.

    Returns the gap text on the call that reaches
    ``EMPTY_RESULT_ALERT_STREAK`` and ``None`` otherwise, so one broken channel
    is reported once per streak instead of on every later query. The two
    wordings keep "this channel never covered these queries" apart from "this
    channel used to produce and then went quiet"; only the second one is
    evidence that the channel itself is broken.

    A provider error breaks the streak without being counted here: it already
    has its own failure record and its own quarantine, and mixing the two would
    report a channel that is loudly broken as a channel that is quietly empty.
    """
    empty_streaks = run.setdefault('provider_empty_streaks', {})
    if status != 'ok':
        empty_streaks[provider_name] = 0
        return None
    if results:
        empty_streaks[provider_name] = 0
        totals = run.setdefault('provider_result_totals', {})
        totals[provider_name] = totals.get(provider_name, 0) + 1
        return None
    streak = empty_streaks.get(provider_name, 0) + 1
    empty_streaks[provider_name] = streak
    if streak != EMPTY_RESULT_ALERT_STREAK:
        return None
    if run.get('provider_result_totals', {}).get(provider_name, 0):
        return (f'检索通道 {provider_name} 连续 {streak} 次返回 0 结果（该通道本轮此前有产出），'
                '疑似通道故障或被限流；本轮结论没有使用它的任何证据。')
    return (f'检索通道 {provider_name} 连续 {streak} 次返回 0 结果且本轮从未有产出，'
            '可能并不覆盖这类查询；请不要把它当成"已经检索过"。')


def extend_gaps_once(run, warnings):
    """把一次检索的告警并进 ``run['gaps']``，通道级告警同句只留一条。

    实测（``sample-b-brand1``）：同一轮里 ``deepseek_web`` 三次空返回写了三条一模一
    样的缺口，用户看到的就是同一句话刷三遍。通道级告警按通道去重；其余告警原样追加，
    行为一个字没改。
    """
    gaps = run.setdefault('gaps', [])
    for warning in warnings or []:
        text = str(warning)
        if text.startswith(_CHANNEL_GAP_PREFIXES) and text in gaps:
            continue
        gaps.append(text)
    return gaps


def irrelevant_document(text):
    """这份已抓到的附件正文是不是与采购无关的文书（True = 别再喂给模型）。"""
    if not isinstance(text, str) or not text.strip():
        return True
    head = text[:DOCUMENT_HEAD_CHARS]
    return any(marker in head for marker in NOISE_DOCUMENT_MARKERS)


def gap_label(text):
    """这一条缺口该贴什么标签（说不清时用中性的"说明"，不写"待核实"）。"""
    for pattern, label in GAP_LABELS:
        if pattern.search(text or ''):
            return label
    return '说明'


def _related_to_target(run, source, leads):
    """这份来源值不值得花额度读它的附件（设计规范：「先确保有相关型号的
    公告再抓取配置附件」）。

    三条窄路，任一成立即可：
      1. 正文里含**基线型号或本轮任一候选型号**——候选品的资料页也在这一档，
         它自己的附件（产品手册之类）正是候选配置的来源。只认基线型号会把它
         判成"无关"：灭菌锅那轮 ``www.sh-boxun.cn/bxm/BXM-110VF.html`` 是候选
         型号 BXM-110VF 的参数页，而基线是 HVE-50，实测按基线判会被挡掉；
      2. 正文含**品牌 + 品类**（同一台仪器的采购，正文只是没写全型号）；
      3. 附件文件名里含上面任一型号（型号只写在附件名里）。

    "来源类型是招标文件"不再单独放行：按关键词捞回来的**别的项目**的文件也满足那一条。
    批3 实测：那条旧闸门放进了一份 77356 字的"智慧互联白板一体机"招标文件和一份
    26413 字的马铃薯基地项目文件，通篇目标型号出现 0 次，额度白花。
    """
    baseline = run.get('baseline') or {}
    body = source.get('body_text') or ''
    models = [str(baseline.get('model') or '').strip()]
    models += [str(row.get('model') or '').strip()
               for row in run.get('alternatives') or [] if isinstance(row, dict)]
    models = [model for model in models if model]
    if any(contains_model(body, model) for model in models):
        return True
    brand = str(baseline.get('brand') or '').strip()
    category = str(baseline.get('category') or '').strip()
    if brand and category and brand in body and category in body:
        return True
    for lead in leads or ():
        text = '%s %s' % (lead.get('label') or '', lead.get('url') or '')
        if any(contains_model(text, model) for model in models):
            return True
    return False


def failure_kind_text(kind):
    """把失败代号写成给人看的说法。

    ``failure_kind`` 是内部代号，它会拼进 ``error_reason``，再作为缺口印在报告里，
    所以这里必须翻译。代号集合本身就是开放的——兜底那一条来自 ``type(cause).__name__``，
    任何异常类名都可能出现——因此认不出的统一说「读取失败」，而不是继续往外拼英文类名。
    """
    value = str(kind or '').strip()
    if not value:
        return '读取失败'
    if value in FAILURE_KIND_LABELS:
        return FAILURE_KIND_LABELS[value]
    if re.fullmatch(r'http_\d{3}', value):
        return f'来源返回错误状态（HTTP {value[5:]}）'
    if value.startswith('tls_'):
        return '安全连接失败'
    return '读取失败'


def failure_location(error):
    """出错位置，只进 ``run['failures']``，不面向用户（见 ``note_failure``）。"""
    import traceback
    trace = traceback.extract_tb(getattr(error, '__traceback__', None))
    if not trace:
        return None
    return {'function': trace[-1].name, 'line': trace[-1].lineno,
            'file': trace[-1].filename.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]}


def _blank_query_values(match):
    """把一条 URL 查询串里每个 ``k=v`` 的**值**抹成 ``***``，参数名留着。

    为什么不连名字一起丢（``设计规范`` 侧原是把整条查询串换成 ``?<已隐去>``）：名字
    留着才看得出"这条链接当时带了哪几个参数"；要证明的只是凭据没落库，不是把请求
    形状也抹平。

    为什么不按名字表来：名字表是白名单，漏一个就是一条凭据原样落库（``api_key``
    就这么漏过）。这里"值一律不看、全抹"，认不出的参数名也一样抹得掉。
    """
    pairs = []
    for segment in match.group(2).split('&'):
        if '=' in segment:
            pairs.append(segment.split('=', 1)[0] + '=***')
        else:
            pairs.append(segment)
    return match.group(1) + '?' + '&'.join(pairs)


def redact_error_text(text):
    """异常原文进留痕之前的脱敏：抹掉凭据的值，再抹号码。

    为什么要有这一步：``str(error)`` 里带着出问题的主机名（这正是我们要的定位
    信息），但也可能带着整条 URL 与它的查询串。**留痕要能定位到具体那一条链接，
    但不能把凭据一起落库**。

    三层，缺一不可：①按名字抹（含 ``api_key`` 这类绕过旧名单的写法）；②对 http(s)
    地址的查询串按"值全抹"兜底（认不出的名字也漏不掉）；③``mask_landed_text``
    抹手机号与 18 位号。载荷与实测见 ``tests/test_lane_crash_resilience.py`` ③ 一节。
    """
    from backend.services.education_platform_sources import mask_landed_text
    if not isinstance(text, str):
        text = str(text)
    value = _ONE_TIME_TOKEN.sub(lambda m: m.group(1) + m.group(2) + '***', text)
    value = _URL_QUERY.sub(_blank_query_values, value)
    if len(value) > 4000:
        value = value[:4000] + '…'
    return mask_landed_text(value)


def note_failure(run, stage, code, error):
    """把一次失败的技术细节留在 ``run['failures']``。

    这些字段（异常类名、出错行、**异常原文**）是给排查用的，**不渲染到页面**
    （前端全库搜不到 ``failures``，见 ``设计规范`` 第 3 节），所以异常类名放这里、
    缺口文字那一侧只写中文。

    ``error`` 记的是脱敏后的 ``str(error)``。为什么非记不可：标准库
    ``urllib/parse.py:_checknetloc`` 抛 ``ValueError`` 时，**message 里就带着那个
    出问题的主机名**；只记 ``type`` 与 ``location`` 的话，事后谁也回不到具体是
    哪一条链接（``output/lane-crash`` 里那一轮就是这样，只能把页面重新抓一遍才
    定位到）。
    """
    run.setdefault('failures', []).append({'stage': stage, 'type': type(error).__name__,
                                           'code': code, 'location': failure_location(error),
                                           'error': redact_error_text(error)})


def document_quality_gaps(documents):
    """上传资料「页面识别失败、文字层却仍然抽出了正文」时的诚实提示。

    实测（``eval-sample`` 的 B02 那一轮）：``POST /tasks/{id}/documents`` 返回
    ``ocr_status=failed``，同一份资料的文字层随后仍抽出了 40021 字，并被当成研究
    输入用掉了——而报告里一个字都没提。读者因此无从判断这些文字是"整份资料"还是
    "只读得出来的那一部分"，也无从判断质量。

    判据只有两条，都是文档记录自己写着的事实，对内容不做任何新判断：
    ``ocr_status`` 是 ``failed``/``partial``，且 ``extracted_text`` 非空。
    只有识别失败（或部分完成）**但仍**产出了文字的才提示；识别失败且一个字都没有的
    那份由"资料没有正文"那条既有链路说话，这里不重复。
    """
    out = []
    for document in documents or []:
        if not isinstance(document, dict) or document.get('ocr_status') not in {'failed', 'partial'}:
            continue
        text = document.get('extracted_text') or ''
        if not text.strip():
            continue
        name = document.get('source_name') or document.get('source_uri') or document.get('id') or '（未命名资料）'
        state = '页面识别（OCR）失败' if document['ocr_status'] == 'failed' else '页面识别（OCR）只完成了一部分'
        out.append(f'{name}：{state}，但它的文字层仍抽出了 {len(text)} 字并已用于本次分析；'
                   '这些文字可能只是资料的一部分、质量未经确认，涉及它的参数与配置请核对原件。')
    return out


def document_quality_notice(run, documents):
    """把 ``document_quality_gaps`` 的提示补进 ``run['gaps']``（幂等）。"""
    for gap in document_quality_gaps(documents):
        if gap not in run['gaps']:
            run['gaps'].append(gap)


def _human_gap(text):
    """把一条缺口写成给人看的中文；认不出的原样返回（它是中文句子或来源网址）。"""
    value = str(text or '').strip()
    for pattern, replacement in GAP_TOKENS:
        value = pattern.sub(replacement, value)
    return value


def _human_fact(label, value):
    """一条 fact；值是空的、``unknown`` 的直接不要（前端那边同样会当成没有）。"""
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text or text.lower() in {'unknown', 'none', 'null', 'n/a'}:
        return None
    return {'label': label, 'value': text}


def report_blockers(run):
    """总述里的未完成事项：与「证据缺口」节**同一条管线**，只是已经换成中文说法。

    总述默认展开在页面最上面，是"任何一层"里最显眼的一层，所以它必须和「证据缺口」
    节走**同一套**取舍与**同一套**翻译：先 ``reconcile_tender_body_gaps``（正文已经读到
    的 ``tender_body_unverified`` 缺口、以及带着旧版阶段注记的那条，两节都不再印，
    否则总述说一条、缺口节说另一条，读者不知道信哪个），再 ``_human_gap`` 与
    ``humanize_gap_text``——``_human_gap`` 只认具体代号，异常类名（``（TimeoutError）``）
    得靠 ``humanize_gap_text`` 的兜底规则。少了翻译这一步，缺口节是人话、总述里还是
    ``TimeoutError``（``test_no_internal_symbol_survives_anywhere_in_the_report`` 抓住）；
    少了取舍这一步，同一批缺口在两处一多一少
    （``test_the_summary_blockers_and_the_gap_section_say_the_same_thing`` 抓住）。
    """
    from backend.services.report_library import humanize_gap_text
    reconciled = reconcile_tender_body_gaps(run.get('gaps'), run.get('sources'))
    return [gap for gap in dict.fromkeys(humanize_gap_text(_human_gap(item))
                                        for item in reconciled) if gap]


def hidden_hosts(run):
    """这一轮判定为不列出的域名集合（脱敏与报告卫生共用一处判据）。"""
    sources = run.get('sources')
    if not isinstance(sources, list):
        return set()
    hidden = {host_of(source.get('url')) for source in sources
              if isinstance(source, dict) and source.get('authority') == 'unlisted'}
    hidden.discard('')
    return hidden


def redact_unlisted_urls(target, hidden=None, placeholder='#'):
    """保留来源 URL，并恢复旧展示副本里已经被替换成 ``#`` 的地址。

    以前这里会把 ``unlisted`` 来源改成 ``#``，导致用户无法打开候选原文，也让前端
    无法继续使用候选来源。现在来源身份、报告文字和价格采信规则各自表达其判定，URL
    本身对用户可见。参数保留是为了兼容旧调用点；历史数据中已经写成
    ``{'url': '#', 'original_url': 'https://...'}`` 的节点会在展示时恢复。

    ``target`` 可以是 run，也可以是报告；函数保持幂等，不修改正文引文。
    """

    def scrub(node):
        if isinstance(node, dict):
            url = node.get('url')
            original_url = node.get('original_url')
            # 历史 run 已把真实地址放进 original_url；展示时恢复，避免旧证据永久变成 #。
            if (isinstance(url, str) and url == placeholder and
                    isinstance(original_url, str) and parse_web_url(original_url)):
                node['url'] = original_url
            for value in node.values():
                scrub(value)
        elif isinstance(node, list):
            for value in node:
                scrub(value)

    scrub(target)
    return target


_REPORT_URL_IN_TEXT = re.compile(r'https?://[^\s，。；、）)"\'\\]+')


def _mentions_unlisted(text, hidden):
    """这段文字说的是不是"这一轮判定为不列出的来源"。

    两种命中方式：
    1. 域名在本轮 ``hidden`` 里（``run.sources`` 打标的结果）——这是主判据；
    2. 域名在后端**已注册的聚合站表**里。必要的兜底：报告正文里会出现"检索阶段
       看到过、研究阶段没纳入来源清单"的地址（实测 ``biaoxun.shuidi.cn`` 就是
       这一条），它们没有来源记录、拿不到标记，不兜底就会一直印在页面上。

    兜底刻意**只认注册表**，不按"未知域名一律排除"处理：只凭 URL 判定会把厂家官网
    也判成不列出（``beckman.com``、``xiangyilxj.com`` 没有正文证据时正是如此），
    而那些厂家页面恰恰是参数依据，是要留下的。
    """
    if not isinstance(text, str) or not text:
        return False
    if any(word in text for word in TRANSSHIPMENT_WORDS):
        return True
    if any(host in text for host in hidden):
        return True
    return any(is_marketplace_or_aggregator(host_of(url)) for url in _REPORT_URL_IN_TEXT.findall(text))


def _doomed_entry(entry, hidden):
    if isinstance(entry, str):
        return _mentions_unlisted(entry, hidden)
    if isinstance(entry, dict):
        return any(_mentions_unlisted(entry.get(key), hidden) for key in ENTRY_TEXT_KEYS)
    return False


def _drop_clauses(text, hidden):
    """整句丢掉，不把句子拆成半截——只删命中的那个子句，其余照原样拼回去。"""
    parts = re.split(r'(?<=[，。；！？])', text)
    kept = [part for part in parts if not _mentions_unlisted(part, hidden)]
    if len(kept) == len(parts):
        return text
    return ''.join(kept)


def _sanitize_node(node, hidden):
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            if isinstance(value, list):
                cleaned[key] = [_sanitize_node(item, hidden) for item in value
                                if not _doomed_entry(item, hidden)]
            elif isinstance(value, str):
                cleaned[key] = _drop_clauses(value, hidden)
            else:
                cleaned[key] = _sanitize_node(value, hidden)
        return cleaned
    if isinstance(node, list):
        return [_sanitize_node(item, hidden) for item in node if not _doomed_entry(item, hidden)]
    return node


def _clean_report_text(text: str) -> str:
    from backend.services.report_library import humanize_dict_literals
    cleaned = humanize_dict_literals(text)
    cleaned = re.sub(r'\bconfiguration_model_basis\b', '型号归属依据', cleaned)
    cleaned = re.sub(r'\bconfiguration_model_gate\b', '型号归属核验记录', cleaned)
    return cleaned


def _clean_report_node(node):
    if isinstance(node, dict):
        return {k: _clean_report_node(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_clean_report_node(item) for item in node]
    if isinstance(node, str):
        return _clean_report_text(node)
    return node


def sanitize_report(report, run):
    """按当前取材口径清一遍**存下来的**报告，只改内存里的展示副本。

    报告是建它那一刻写下的，措辞和取材都固化了，而读报告这条路（``routes`` 里的
    ``list_reports``）直读库、不重算。``task-013dcdcff7e6`` 的报告就是这样：
    ``sections`` 里那一节已经被 ``refresh_republished_section`` 删掉，``summary``
    里却还留着「二手成交记录（聚合站转载）9 条」这条事实、总述里还写着「另有 9 条
    二手成交记录来自聚合站转载」、未完成事项里还列着那些地址——页面上照样看得见。

    判据与来源清单、页面、报告那一节同一个：**这个任务有没有可信的官方成交记录**。
    有，则聚合站口径的文字与地址在页面上一个不留；没有，则它们正是要展示的内容
    （用户要靠它们核对），原样返回。
    """
    if not isinstance(report, dict):
        return report
    report = _clean_report_node(report)
    mark_source_authority(run)
    if not has_award_record(run):
        return report
    hidden = hidden_hosts(run)
    redact_unlisted_urls(report, hidden)
    return _sanitize_node(report, hidden)


def run_brands(run):
    """本轮在用的品牌，已按 ``identity`` 归一化。

    判定厂家官网要用品牌名，而国内厂家官网的域名是拼音、页面上写的是中文，
    所以判据分两步走：版权方公司名、以及 ``source_authority`` 里那张已知的
    品牌→域名词表。品牌名从这里一次收齐（基准 + 替代候选 + 发现候选），
    免得三处各收一份、各漏一部分。
    """
    names = []
    baseline = run.get('baseline') or {}
    if baseline.get('brand'):
        names.append(baseline['brand'])
    for candidate in list(run.get('alternatives') or []) + list(run.get('discovered_candidates') or []):
        if isinstance(candidate, dict) and candidate.get('brand'):
            names.append(candidate['brand'])
    return [name for name in dict.fromkeys(identity(n) for n in names) if name]


def mark_source_authority(run):
    """给 ``run['sources']`` 每条打上展示身份，只在内存里改。

    ``authority`` 就是 ``source_authority.source_display_authority`` 的结论
    （``official_channel`` / ``manufacturer`` / ``unlisted``），**逐字照抄、不再折叠**：
    前端既用它决定来源清单列不列（``unlisted`` 不列），也用它把清单分成
    「采购公告与合同／官方渠道与发布机构／厂家资料」。早先这里只写了 ``listed`` /
    ``unlisted`` 两种值，前端拿不到"是官方渠道还是厂家"，机构站点全都无处归类。

    主机粒度汇总：同一个域名下取**最高**那一档（官方渠道 > 厂家 > 不列），
    同一份公告的正文与附件不该在清单里一半出现一半消失。

    ``run['sources']`` 里存着整页正文，几十次落库都跑一遍这个判定是白花钱，
    所以只在 ``display()``（读给页面看的那一条路）调用，不写回库里。
    """
    from backend.services.source_authority import source_display_authority
    sources = run.get('sources')
    if not isinstance(sources, list) or not sources:
        return run
    brands = run_brands(run)
    rank = {'official_channel': 2, 'manufacturer': 1, 'unlisted': 0}
    per_host = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        verdict = source_display_authority(source, brands)
        host = host_of(source.get('url'))
        if rank[verdict] > rank[per_host.get(host, 'unlisted')]:
            per_host[host] = verdict
    for source in sources:
        if isinstance(source, dict):
            source['authority'] = per_host.get(host_of(source.get('url')), 'unlisted')
    return run


def brand_group_key(value):
    """品牌分组建模用的键：别名表归一后的写法；没有品牌时给空串。"""
    return facade_attr('brand_identity', brand_identity)(value) if isinstance(value, str) and value.strip() else ''


def annotate_candidate_brands(run):
    """把"这个候选项算不算原品牌"在后端算好，前端不再自己维护一份别名表。

    ``ResearchResults.jsx`` 原来在页面里硬编码了**第三份**别名表（只有
    ``天津泰斯特``／``上海一恒`` 两条，缺 ``徕卡`` 与两条苏州安泰），于是界面上把
    ``徕卡``／``Leica``、``苏州安泰``／``苏净安泰`` 分成两个品牌两组。别名表的唯一
    来源是 ``brand_model_rules._BRAND_ALIASES``，所以分组判据也回到后端算：
    每个候选项补两个字段，前端只展示。

    * ``brand_key``：规范写法，用来把同一品牌的多个型号归到一组；
    * ``same_brand_as_baseline``：是不是与比较基准同品牌，口径与
      ``research_planning.diverse_candidates`` 的 ``same_brand`` 一致（同一个
      ``brand_identity``）。

    只在候选项有品牌写法时给这两个字段；品牌为空时两个字段都不给，前端按
    "品牌信息不完整"那一档处理（与它今天的 ``unknown`` 分组一致）。

    和 ``mark_source_authority`` 一样只在 ``display()`` 这条读路径上算、不写回库：
    它是纯函数，落库的 run 里没有这两个字段也不影响判据——判据读的是 ``brand``
    本身，不是这两个展示字段。
    """
    baseline = run.get('baseline') or {}
    base_key = brand_group_key(baseline.get('brand'))
    for candidate in run.get('alternatives') or []:
        if not isinstance(candidate, dict):
            continue
        # 读接口可能对同一份 run 反复调用，先清掉上一次算出来的值再算。
        candidate.pop('brand_key', None)
        candidate.pop('same_brand_as_baseline', None)
        key = brand_group_key(candidate.get('brand'))
        if not key:
            continue
        candidate['brand_key'] = key
        candidate['same_brand_as_baseline'] = bool(base_key) and key == base_key
    return run


def republished_section(run):
    """「二手成交记录（聚合站转载）」这一节；产出与就地重刷共用一处。

    These rows are read off an aggregator's repost, not off the announcement.
    「公告未提供」 named the wrong document: the measured source page has no
    configuration column at all
    (设备名称/设备品牌/设备型号/采购单位/成交单位/成交时间/成交价格/操作), so the
    honest statement is about the page we actually read.

    引导语原来写死成「已列出采购方、日期与金额……」，可这一节 0 条时它也在印——
    机械核对过 ``republished_summary_claims_listed=True`` 而 ``republished_items=0``。
    有没有列出来由 ``republished_prices`` 决定，不写常量。
    """
    rows = list(run.get('republished_prices') or [])
    summary = ('以下记录由聚合站转载，不是一手公告。已列出采购方、日期与金额；'
               '配置与交易条件请点开原页核对后自行判断可比性。不同配置与年份的成交价差异可能很大，'
               '不建议直接当成同口径区间。' if rows else
               '本轮没有从聚合站转载页读到可用的二手成交记录；'
               '聚合站只是发现渠道，它的页面不能当成一手公告或成交证据。')
    return {'key': 'republished', 'title': '二手成交记录（聚合站转载）',
            'summary': summary,
            'items': [{'label': r.get('purchaser') or '采购方未注明',
                       'text': f"{r['model']}；{format_money(r['unit_price'])} {r.get('currency') or 'CNY'}"
                               f"；日期：{r.get('date') or '未知'}"
                               f"；配置：{r.get('configuration') if r.get('configuration') not in (None, '', 'unknown') else '来源页未提供配置'}"
                               f"；供应商：{r.get('supplier') or '未注明'}"
                               f"；来源等级：二手（聚合站转载页）",
                       'evidence': [{'url': r['url'], 'excerpt': str(r.get('excerpt') or '').strip()[:400],
                                     'evidence_status': 'secondary'}]}
                      for r in rows]}


def report_overview_facts(run, decision):
    """总述的结构化部分：全部从 run 里机械取出，不经过模型。

    机械取数是为了让报告骨架不受模型影响——模型挂了、额度用完了，这几项仍然是对的。
    口径是汇总的：价格条数、日期范围、涉及供应商、主要缺口，不照抄某一节自己的 summary。
    """
    baseline = run.get('baseline') or {}
    prices = award_records(run)
    dates = sorted({str(q['date']) for q in prices if q.get('date')})
    suppliers = list(dict.fromkeys(str(q['supplier']) for q in prices if q.get('supplier')))
    alternatives = run.get('alternatives') or []
    comparable = sum(a.get('comparison_disposition') in {'comparable_candidate', 'recommended_comparison'}
                     for a in alternatives)
    eligible = sum(a.get('status') == 'eligible' for a in alternatives)
    # 没有确认过指标时不能写「其中 N 款全部关键指标满足」：那一条在这里退化成
    # "0 项要求都满足"，会把"没比过"说成"合格"。
    no_metric = (run.get('comparison_basis')
                 or comparison_basis(baseline.get('parameters'))['comparison_basis']) == NO_CONFIRMED_PARAMETER
    supplier_price = baseline.get('supplier_price')
    state = (decision or {}).get('state') or ''
    facts = [
        _human_fact('比较基准', f"{baseline.get('brand') or ''} {baseline.get('model') or ''}"),
        _human_fact('采购用途', baseline.get('scenario')),
        _human_fact('销售报价', ((f"{format_money(supplier_price)} {'元' if baseline.get('currency') == 'CNY' else '（币种尚未说明）'}"
                                  + quoted_model_note(baseline))
                                 if supplier_price not in (None, '') else '未提供')),
        _human_fact('可采信的同型号成交记录', f'{len(prices)} 条'),
        _human_fact('成交日期', _date_range(dates) or '未核实'),
        _human_fact('成交供应商', ('、'.join(suppliers[:3]) + (' 等' if len(suppliers) > 3 else '')) or '未注明'),
        *([] if prices else [_human_fact('二手成交记录（聚合站转载）', f"{len(run.get('republished_prices') or [])} 条")]),
        _human_fact('同档替代候选', ('本轮没有确认必须满足的实验要求，未做逐项参数比对，不判定替代候选是否满足关键指标'
                                     if no_metric else
                                     f'{comparable} 款达到70%已核验相似度，其中 {eligible} 款全部关键指标满足')),
        _human_fact('比价依据', PRICE_STATE_LABELS.get(state, '成交核价状态未记录')),
        _human_fact('未完成事项', f'{len(report_blockers(run))} 条'),
        _human_fact('本轮完成度', '全部完成' if run.get('status') == 'completed' else '部分完成，仍有未核实项'),
    ]
    return [fact for fact in facts if fact]


def report_overview_headline(run, decision):
    """总述的一句话结论：从已保存的数据机械拼出，模型不可用时就是它。

    它只说条数、状态和缺口，不替规则引擎下新判断，也**不重复** ``price_assessment``
    的那句完整判断——报告里同一件事说三遍，读的人只会更糊涂。
    """
    baseline = run.get('baseline') or {}
    model = f"{baseline.get('brand') or ''} {baseline.get('model') or ''}".strip() or '本次比价型号'
    prices = [q for q in (run.get('prices') or []) if q.get('kind') in {'award', 'contract'}]
    if quoted_model_note(baseline):
        return (f'{model}：销售报价对应的是 {baseline.get("quote_model")}，不是本次比较基准；'
                f'底下这些成交记录查的是 {model}，不能用来判断那份报价的高低。'
                f'请先让销售按 {model} 重新报价。')
    if not prices:
        return f'{model}：本轮没有查到可采信的同型号成交记录，暂不能判断销售报价。'
    if (decision or {}).get('state') == 'historical_comparable':
        return (f'{model}：查到 {len(prices)} 条可采信的同型号历史成交记录，'
                '配置口径未核实，不能合并成一个价格区间。')
    return (f'{model}：查到 {len(prices)} 条参考级同型号成交记录，'
            '日期或商务条件尚未核实，暂不能判断销售报价。')


def apply_empty_section_notes(sections):
    """把空节的引导语换成一句能被数据核对的话（幂等）。"""
    changed = False
    for section in sections or []:
        note = EMPTY_SECTION_NOTES.get(section.get('key'))
        if note is None or section.get('items'):
            continue
        if section.get('summary') != note:
            section['summary'] = note
            changed = True
    return changed


def baseline_field_text(baseline, run, key, label=''):
    """比较基准里一项销售口述字段该写什么。

    有值就写值，并标出它是**这一轮销售回复补充**的（补进去的内容要和原有内容分得开，
    盲测第 8a 条：7 个补充字段在报告里看不出来是哪来的）。没值就写清是"没问"还是
    "问了、对方说资料里没有"——这两件事对采购员是两种处境。
    """
    value = baseline.get(key)
    if value not in (None, '', []):
        revised = key in ((run.get('supplier_revision') or {}).get('changes') or {})
        text = str(value)
        return text + '（本轮销售回复补充）' if revised else text
    return missing_information_state(run, key, label)


def baseline_section(run):
    """「比较基准」节。抽成函数是为了让库里已存的报告也能按当前口径重刷。"""
    b = run['baseline']
    quoted_model = str(b.get('quote_model') or '').strip()
    quoted_name = ' '.join(part for part in (str(b.get('quote_brand') or '').strip(), quoted_model) if part)
    quote_price = (f"{b['supplier_price']} {'元' if b.get('currency') == 'CNY' else '（币种尚未说明）'}"
                   if b.get('supplier_price') not in (None, '') else '未提供')
    sales_name = (f"{quoted_name}；配置：{b.get('configuration') or '未提供'}" if quoted_model
                  else f"{b.get('brand') or ''} {b['model']}；配置：{b.get('configuration') or '未提供'}")
    sales_price = (f'{quote_price}（对应销售推荐的 {quoted_name}，不适用于比较基准 {b["model"]}）'
                   if quoted_model and quote_price != '未提供' else quote_price)
    return {'key': 'baseline', 'title': '比较基准',
            'summary': ('本报告仅部分完成。' if run['status'] != 'completed' else '') + b['scenario'],
            'items': [
                *([{'label': '比较基准（产品资料）', 'text': f"{b.get('brand') or ''} {b['model']}；配置以产品资料为准"}]
                  if quoted_model else []),
                {'label': '销售推荐', 'text': sales_name},
                {'label': '销售单价', 'text': sales_price},
                *[{'label': '实验要求 · ' + p['name'],
                   'text': f"{p['value']} {p.get('unit') or ''}" + (f"；条件：{p['conditions']}" if p.get('conditions') else '')}
                  for p in b.get('parameters', [])],
            ] + [{'label': label, 'text': baseline_field_text(b, run, key, label)} for key, label in SALES_FIELDS]}


def supplier_revision_section(run):
    """「销售回复更新」：这次回复**具体补了什么、依据在哪、哪些项对方说没有**。

    盲测第 8 条：补全之后报告里只多出一个 items=0 的空节，7 个字段的改动与它们的
    依据（原句 + 位置）一条都没进报告。这一节把 ``supplier_revision`` 原样摊开，
    逐项写清字段、值、依据；答复与"资料中未提供"各列一项。
    """
    revision = run.get('supplier_revision') or {}
    if not revision:
        return None
    labels = dict(SALES_FIELDS)
    labels.update({'configuration': '本次报价配置', 'supplier_price': '含税销售单价',
                   'quote_model': '报价对应的型号', 'quote_brand': '报价对应的品牌',
                   'preferences': '功能偏好'})
    changes = revision.get('changes') or {}
    evidence = revision.get('field_evidence') or {}
    items = []
    for key, value in changes.items():
        text = str(value)
        if key in evidence:
            detail = evidence[key]
            quote = str(detail.get('quote') or '').strip()
            location = str(detail.get('location') or '').strip()
            parts = [part for part in (f"原句：{quote}" if quote else '',
                                       f"位置：{location}" if location else '') if part]
            if parts:
                text += '；依据：' + '；'.join(parts)
        items.append({'label': '本轮补充 · ' + labels.get(key, key), 'text': text})
    for answer in revision.get('answers') or []:
        question = str(answer.get('question') or '').strip()
        text = str(answer.get('answer') or '').strip()
        parts = [f"原句：{answer['quote']}" if str(answer.get('quote') or '').strip() else '',
                 f"位置：{answer['location']}" if str(answer.get('location') or '').strip() else '']
        if parts:
            text += '；依据：' + '；'.join(part for part in parts if part)
        items.append({'label': '答复 · ' + (question or '开放式追问'), 'text': text})
    for key in revision.get('not_provided') or []:
        items.append({'label': '已询问未提供 · ' + labels.get(key, key),
                      'text': '销售已回复：资料中未提供。这一项不必再向销售追问；'
                              '需要这个条件时只能从其他来源取证或另行索取。'})
    if revision.get('reply_notes'):
        items.append({'label': '补充说明', 'text': str(revision['reply_notes'])})
    return {'key': 'supplier_revision', 'title': '销售回复更新',
            'summary': ('原报告保留；以下是这次销售回复按字段列出的事实与原话，'
                        '销售陈述不等于厂家已核实能力。'),
            'items': items}


def gap_action_summary(run):
    """为「证据缺口」配一条"这一轮到底做过什么"的账。

    缺口只写"没查到"时，读者没法判断是我们没查、还是查了没有。这里把做过多少次
    检索、有多少个来源打不开、有多少条线索没能核实，全部按机械计数写出来。
    """
    def count(value):
        # 真实库里这几个字段的形状并不统一（``unresolved_source_errors`` 实测是 ``True``，
        # 不是列表）：按"有没有内容"数，别让计数把报告读挂。
        if isinstance(value, (list, tuple, dict, set)):
            return len(value)
        return 1 if value else 0

    completed = count(run.get('completed_queries'))
    attempts = count(run.get('search_attempts'))
    unavailable = count(run.get('unavailable_sources'))
    errors = count(run.get('unresolved_source_errors'))
    if not (completed or attempts or unavailable or errors):
        return None
    text = f'本轮共留下 {completed} 次检索、{attempts} 次检索尝试的记录。'
    if unavailable or errors:
        text += (f'其中 {unavailable + errors} 次读取没有成功（站点限制、超时或页面打不开），'
                 '地址与原因记在随报告导出的「来源清单」里。')
    return {'label': '本轮为这些缺口做过的查询', 'text': text}


def no_comparable_candidates_gap(run):
    """替代车道跑完、却没有可比较候选时，写给用户的那一句（按真实原因分）。

    从前这里只有一句"本轮没有达到70%已核验相似度的跨品牌候选。"，而它盖住了四种
    完全不同的处境（``设计规范`` §3.4/§5：对外的粒度不许粗于内部事实）：

    * **候选进了名单、一条都没达标**：``alternatives`` 非空——只有这种才谈得上
      "没有达到 70% 已核验相似度"；
    * **候选发现了、一条都没进可比较位**：拦住它们的是身份与名额（品牌读不到、
      配置读不到被撤下、配置明确不符、核验名额已满…），逐条记在
      ``reference_candidates[].reference_reason`` 上——按原因归组说；
    * **一条候选都没发现**：与"没有实验要求"无关，得如实说没从正文里识别出来；
    * 前三者都不适用时沿用原句。

    2026-10-08 实测（``设计规范``）：``output/e2e-final/runs`` 里 76 个 run 快照共
    99 条去重后的 research 记录，其中 **69 条**带这句旧话，而**没有一条**真有候选
    算出过 ``similarity_score``（参照位原因合计：brand_unresolved 252、
    verification_limit 225、config_hard_constraint 62、supplier_already_represented 45、
    brand_already_represented 26、same_brand 9）——读者据此会以为"再像一点就能进
    比较"，而真实原因是身份/证据缺失或配置明确不符。

    原因码只显示一个（``research_planning.diverse_candidates`` 把品牌判定放在硬约束
    之前），但同一条线索可能**同时**存在品牌读不到与配置不符两件事，只报一个就是把
    事实说少一半（``b281-hve50``：11 条里 8 条"品牌未解析"，其中 7 条的配置也比对出
    有效容积不符），所以两句都要说。
    """
    if [a for a in (run.get('alternatives') or []) if isinstance(a, dict)]:
        # 候选确实进了名单：这时才谈得上"没有达到 70% 已核验相似度"。只有真算过分数
        # 的那些才把"逐项核验"说出来；没算过分的（老记录、矩阵还没建）沿用原句，
        # 因为它说的是"名单里没有达标的候选"，不是"算出了 69%"。
        scored = [a for a in run['alternatives']
                  if isinstance(a, dict) and a.get('similarity_score') is not None]
        if scored:
            return '本轮有 %d 条候选做了逐项核验，但没有一条达到70%%已核验相似度。' % len(scored)
        return '本轮没有达到70%已核验相似度的跨品牌候选。'
    references = [c for c in (run.get('reference_candidates') or []) if isinstance(c, dict)]
    if references:
        counts = {}
        for candidate in references:
            reason = str(candidate.get('reference_reason') or '')
            counts[reason] = counts.get(reason, 0) + 1
        listed = '；'.join(
            '%s（%d 条）' % (REFERENCE_REASONS.get(reason, '原因未记录'), count)
            for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
        sentence = ('本轮发现了 %d 条候选线索，但没有一条进入可比较位：%s。'
                    '这些线索连同原因留在候选清单里，没有被删除；'
                    '这不等于市场上没有同类替代品。' % (len(references), listed))
        mismatch = [c for c in references if (c.get('config_match') or {}).get('hard_failures')]
        if mismatch:
            names = []
            for candidate in mismatch:
                for name in (candidate.get('config_match') or {}).get('hard_failures') or []:
                    if name and str(name) not in names:
                        names.append(str(name))
            sentence += ('此外，这 %d 条里有 %d 条的配置比对出与已确认要求不符的项（%s），'
                         '即使品牌读到了也进不了可比较位。'
                         % (len(references), len(mismatch), '、'.join(names[:3])))
        return sentence
    if [c for c in (run.get('discovered_candidates') or []) if isinstance(c, dict)]:
        return ('本轮发现了候选，但没有一条进入可比较位（逐条原因见发现记录）；'
                '这不等于市场上没有同类替代品。')
    return ('本轮没有从已取得的正文中识别出其他品牌的候选；这不等于市场上没有同类替代品——'
            '本轮窗口里没有取到可用的同类产品页时，不代表这类产品不存在。')


def _matrix_line_text(row):
    """报告里正式候选的**一行**逐项结果；已判"不满足"的行再补一句"差多少"。

    差额与"是否满足"是两件事（口径见 ``research_planning.parameter_shortfall``）：
    这里只把差额补在已经判成 ``violated`` 的那一行后面，不改 ``status``、不改
    ``observed``，也不动 ``matrix`` 本身。2026-09-29 修的是这个缺口——正式候选
    （``run['alternatives']``，用户最关心的那批）原来只印"要求 X，查得 Y；不满足"，
    看不到"差 3% 还是差 50%"，而发现阶段算好的差额只印在参考候选那一段
    （``verified=False`` 分支），有正式候选时那一段根本不出现在报告里。
    """
    from backend.services.research_planning import shortfall_sentence
    required = row['required']
    observed = row.get('observed') or {}
    text = '%s：要求 %s %s，查得 %s；%s' % (
        required['name'], required['value'], required.get('unit', ''),
        observed.get('value', '未知'),
        {'satisfied': '满足', 'violated': '不满足',
         'unknown': '页面上没写到'}.get(row.get('status'), '页面上没写到'))
    if row.get('status') == 'violated':
        text += '；' + shortfall_sentence(required, observed)
    return text


def _candidate_configuration_text(candidate):
    """候选自己那份标称参数（取到才印）：与成交价行同一个渲染函数、同一套字段。

    2026-10-03 之前候选这一格一直是空的：候选的配置只在"逐项对齐"里出现，而那一层
    是从候选详情页正文现读的，读不出就全是"未知"。产品页接上之后把参数写进候选，
    报告与卡片两处显示的就是同一件事（出处也一起带着，读者能分辨它不是公告写的）。
    """
    if not isinstance(candidate, dict) or not candidate.get('configuration'):
        return ''
    from backend.services.product_configuration import report_configuration
    # 与前半句之间用**句号**：前半句结尾本来就带句号或分号，再接分号会连出"；；"。
    return '。' + report_configuration(candidate)


def _skipped_requirements_text(candidate):
    """被跳过、没参与逐项对齐的要求（配件、纯单位写法）；没有就返回空串。

    跳过了什么必须说出来：用户看到逐项表里少了一项，得能知道是"这一项不是技术参数"，
    而不是"这一项被悄悄丢了"（口径见 ``research_planning._not_a_parameter``）。
    """
    names = ((candidate.get('config_match') or {}).get('skipped_requirements')) or []
    if not names:
        return ''
    shown = '、'.join(str(name) for name in names[:5])
    more = '等 %d 项' % len(names) if len(names) > 5 else ''
    return '。另有 %d 项是配件或单位写法（%s%s），未参与逐项对齐' % (len(names), shown, more)


def alternative_report_item(candidate, verified):
    """报告里「同档替代核查」一节的单条候选。

    ``verified=True``：走过逐项核验、进了跨品牌名额的候选（带 ``matrix``）。
    ``verified=False``：没进名额的参考候选（品牌没从原文里结构化提取出来、同品牌、
    型号重复或名额已满）。它们**只有原文行和型号、没有核验结果**，所以文案只说
    "没进名额、为什么"，再附上正文行里读到的配置对齐，绝不假装比对过。

    2026-09-27 真跑实测：一轮里 10 条候选全是"原文行里读得出型号、但行里没写品牌"，
    于是全部落进参考名单，而报告只列 ``alternatives``——用户看到的仍是"一条候选都没有"。
    候选明明在库里、报告却不说，这是"零候选"体感的第二个来源。
    """
    identity = candidate.get('brand') or (
        '供应商：%s' % candidate.get('supplier') if candidate.get('supplier') else '品牌未解析')
    label = '%s %s' % (identity, candidate.get('model') or '')
    # 来源与原文行合成**一条**证据：分成两条会多印一遍网址，其中一条还没有 excerpt。
    url = candidate.get('url')
    if not url or url == '#':
        url = candidate.get('original_url')
    evidence = [{'url': url, **({'excerpt': candidate['excerpt']} if candidate.get('excerpt') else {})}] if url else []
    if not verified:
        match = candidate.get('config_match') or {}
        text = '本条未进入本轮跨品牌核验名额（%s）。' % REFERENCE_REASONS.get(
            candidate.get('reference_reason'), '原因未记录')
        text += ('正文里读到的配置对齐：' + '；'.join(match['diff_points'])) if match.get('diff_points') \
            else '正文里没有读到可与配置要求逐项对齐的内容。'
        # 留痕要**排在配置前面**：配置那一段动辄上千字，报告行一截断就把留痕挤没了
        # （实测 r8 的三条候选行都截在配置中间，"跳过了 Hz" 没印出来）。
        text += _skipped_requirements_text(candidate)
        text += _candidate_configuration_text(candidate)
        return {'label': label, 'text': text, 'evidence': evidence}
    status_text = (candidate.get('conclusion')
                   or ('关键参数满足' if candidate.get('status') == 'eligible'
                       else '关键参数不满足' if candidate.get('status') == 'ineligible'
                       else '同档候选'))
    matrix = candidate.get('matrix') or []
    detail = ';'.join(_matrix_line_text(row) for row in matrix)
    # 核验那一段一条可比结论都没有时（2026-09-28 两次真跑实测：正式候选的 matrix
    # 三行全是"待核实"、observed 全是 None），发现阶段从详情页正文里逐项读到的结果
    # （含差额）是这一段唯一有值的证据，附上；核验已经给出满足/不满足时不附——同一份
    # 报告里并排两套数字，读者不知道信哪个（两条路的数据源与判据本来就不同：
    # matrix 走跨品牌核验的官方来源，config_match 走候选详情页正文）。
    if matrix and not any(row.get('status') in ('satisfied', 'violated') for row in matrix):
        points = (candidate.get('config_match') or {}).get('diff_points') or []
        if points:
            detail += ('；核验阶段没读到可判定的参数值，附详情页正文里读到的配置对齐：'
                       + '；'.join(points))
    # ``detail`` 为空时不留一个光秃秃的分号（"关键参数待核实；；产品资料…"就是这么来的）。
    # 留痕紧跟结论，然后才是逐项明细与配置（明细与配置都可能很长，报告行一截断
    # 就把后面的话挤没；实测 r8 的候选行就是这么丢掉"跳过了 Hz"那句的）。
    text = status_text + _skipped_requirements_text(candidate) + ('；' + detail if detail else '')
    text += _candidate_configuration_text(candidate)
    return {'label': label, 'text': text,
            'evidence': evidence + [
                {'url': item.get('url'), 'excerpt': item.get('excerpt'),
                 'evidence_status': item.get('evidence_status', 'unparsed')}
                for row in candidate.get('matrix') or [] for item in row.get('raw_evidence') or []
                if item.get('url') and item.get('excerpt')]}
