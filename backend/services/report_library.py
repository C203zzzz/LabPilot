"""Report library metadata and portable, readable batch exports."""
from datetime import datetime, timezone, timedelta
from io import BytesIO
import json
import re
from zipfile import ZipFile, ZIP_DEFLATED
from pathlib import Path
import hashlib

from backend.services.task_title import task_title, instrument_names


#: 库里存下来的缺口是**内部代号**（检索阶段写下的）。报告正文、页面上那份展示副本、
#: 导出的 Markdown 三层必须用同一套翻译，否则同一句话三种说法——盲测第一版报告里
#: 就出现了 ``tender_body_unverified: <url>``、``120shop 遇到人机验证``、
#: ``官方复核接口报错（TimeoutError）`` 这些只对开发有意义的写法。
#:
#: **这张表与前端 ``frontend/src/services/publicText.js`` 是同一张表的两份实现**
#: （前端是 JS，后端是 Python，跨语言没法共用代码）。前端那份一直在生效，所以页面上
#: 是人话；导出报告这条路径原先没接上，于是同一个缺口在页面是人话、导出成机器腔。
#: 现在两边覆盖同一批类别，并且由一份跨端共用的用例清单里那份
#: **同一批真实用例**在两端各跑一遍：任何一边漏了新类别，自己那端的测试就先红。
#: 加规则时的顺序纪律（照抄前端注释）：长的排短的之前，``not_found`` 必须在
#: ``dns_not_found`` 之后，``empty_*`` 同理；只认下划线形态，别啃掉正常英文里的
#: "not found"。
def _gap_channel_names(value):
    """代号里带的通道名（``deepseek_web,bocha``）写成人话里的顿号列表。"""
    parts = [part for part in re.split(r'[,\s]+', str(value or '').strip()) if part]
    return '、'.join(parts)


def empty_results_all_channels_text(match):
    """**跑过的**通道全部没有结果——它才是"整轮检索没有结果"。

    措辞带"实际跑过的"是因为 ``OfficialSearchService`` 会跳过 ``supports`` 拒绝这次
    查询的适配器（运行时会留一个 fetch-only 通道）；把没跑的通道也算进"全部"，
    这句话就又比内部事实粗了。
    """
    names = _gap_channel_names(match.group(1))
    count = len([part for part in re.split(r'[,\s]+', str(match.group(1) or '').strip()) if part])
    return f'这一次查询实际跑过的 {count} 个检索通道（{names}）全部没有返回结果'


def search_channel_failed_text(match):
    """通道抛异常：说失败，不说"没有结果"。"""
    return (f'检索通道 {_gap_channel_names(match.group(1))} 这一次查询失败，没有取得结果；'
            '这是通道故障，不是“没有结果”')


def empty_results_channel_text(match):
    """只有部分（或一个）通道没有结果：只说列出的通道，别读成整轮。"""
    return (f'检索通道 {_gap_channel_names(match.group(1))} 这一次查询没有返回结果'
            '（这一句只说列出的通道，不代表整轮检索没有结果）')


GAP_CODE_LABELS = (
    (re.compile(r'tender_body_unverified:\s*'), '采购公告正文待核实：'),
    (re.compile(r'no_official_source'), '这一次检索未取得官方来源'),
    # 带通道名的空结果/失败（``official_search.empty_result_warnings`` 写的代号）必须
    # 排在裸 ``empty_results`` 之前：否则裸规则会先把代号啃掉一半，通道名裸露给读者。
    (re.compile(r'empty_results_all_channels:\s*([A-Za-z0-9_.,\-]+)'), empty_results_all_channels_text),
    (re.compile(r'empty_results:\s*([A-Za-z0-9_.,\-]+)'), empty_results_channel_text),
    (re.compile(r'empty_results'), '这一次检索没有返回任何结果'),
    # 通道失败与 ``provider_failed`` 分开写：前者是对外的一句"这个通道这次没跑通"，
    # 后者带异常原文、是给排查用的那一份。
    (re.compile(r'search_channel_failed:\s*([A-Za-z0-9_.,\-]+)'), search_channel_failed_text),
    (re.compile(r'provider_failed:[^\s:]+:\s*'), '检索服务未完成：'),
    # 产品自己生成的失败代号（后端把 failure_kind 原样拼进缺口文字）。
    (re.compile(r'\bunsupported_source_url\b'), '这类来源地址暂不支持自动读取'),
    (re.compile(r'\bparse_incomplete\b'), '正文解析不完整'),
    (re.compile(r'\bpage_too_large\b'), '页面过大'),
    (re.compile(r'\bdns_not_found\b'), '域名解析失败'),
    (re.compile(r'\bempty_body\b'), '来源正文为空'),
    (re.compile(r'\bempty_shell\b'), '页面只有空壳'),
    (re.compile(r'\bnot_found\b'), '来源已不存在'),
    (re.compile(r'\bgaierror\b'), '域名解析失败'),
    (re.compile(r'\bherror\b'), '域名解析失败'),
    (re.compile(r'\bRemoteDisconnected\b'), '连接被对方中断'),
    (re.compile(r'\bhttp_\d+'), '来源返回错误状态'),
    (re.compile(r'\btls_[A-Za-z0-9_.]+'), '与来源网站的安全连接未建立'),
    # 异常类名（``（PageReadError）``）对采购人员没有信息量；真正的细节留在 run 的
    # 失败记录里。"占满整个代号位"的要求：前面必须是行首/全角括号/冒号，否则会误伤
    # 正文里的 ``HTTP Error 403``、``WinError 10054``（形状一模一样，分不开）。
    # 措辞与前端 ``publicText.js`` 的兜底规则逐字相同。
    (re.compile(r'(^|[（(：:])\s*[A-Za-z0-9]*(?:Error|Exception|Timeout|Disconnected|Refused'
                r'|Reset|Aborted|IncompleteRead|Failure|Warning|Interrupted)\b'),
     r'\1来源读取未完成'),
    # 这一轮检索与研究分两次跑，旧版会把"检索阶段的判定"自注挂在缺口后面，读者看到
    # 的是报告在解释自己的工作流。源头已改（``reconcile_tender_body_gaps``），这条
    # 收拾已经存进库的旧记录。
    (re.compile(r'（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）'), ''),
    # 只换"裸代号"；``医价网（120shop.cn）`` 这种已经写好的人话名字不再动。
    (re.compile(r'(?<![\w.])120shop(?![\w.])'), '医价网（120shop.cn）'),
    (re.compile(r'(?<![\w.])bidcenter(?![\w.])'), '采招网（bidcenter.com.cn）'),
    (re.compile(r'(?<![\w.])chinamae(?![\w.])'), '中国机电设备采购平台（chinamae.com）'),
    # 内部编号与字段名（``M07``、``fact_id/evidence_id``、``硬约束``）。
    (re.compile(r'M\d{2}(?:/M\d{2})*'), '选型'),
    (re.compile(r'fact_id(?:/evidence_id)?|evidence_id'), '来源记录'),
    (re.compile(r'硬约束'), '必须满足的条件'),
    (re.compile(r'软约束'), '优先考虑的条件'),
    # 枚举值（与前端同一份中文标签）。
    (re.compile(r'\b(eligible|ineligible|no_recommendation|needs_review|unknown|conflicted'
                r'|budget_max|throughput|capability)\b'),
     None),
)

#: 枚举值 → 中文（前端 ``publicText.js`` 里同名的一份）。
ENUM_LABELS = {
    'eligible': '符合条件',
    'ineligible': '不符合条件',
    'no_recommendation': '暂不推荐',
    'needs_review': '需要复核',
    'unknown': '资料未提供',
    'conflicted': '存在冲突',
    'budget_max': '预算上限',
    'throughput': '样品处理量',
    'capability': '功能要求',
}


def humanize_gap_text(text):
    """把缺口文字里的内部代号翻成人话（展示层用；库里那份不动）。

    与前端 ``publicText.js`` 覆盖同一批类别，见 ``GAP_CODE_LABELS`` 上面的说明。
    """
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in GAP_CODE_LABELS:
        if replacement is None:
            # 枚举值按查表替换，别把未知值换成空字符串（那会吃掉一个词，句子读不通）。
            text = pattern.sub(lambda match: ENUM_LABELS.get(match.group(1), match.group(1)), text)
        else:
            text = pattern.sub(replacement, text)
    return text


def evidence_label(evidence):
    """一条证据的出处，写成人能读的样子。

    证据可能指向抓回来的网页，也可能指向**本任务上传的资料**（``document_id`` +
    页码）。后者不能把 UUID 印给读者：报告里那一行要能让人直接去翻第几页
    （``backend.services.product_facts.document_facts`` 把 ``document_name`` 一起写上）。
    """
    if not isinstance(evidence, dict):
        return str(evidence)
    name = evidence.get('document_name') or evidence.get('url') or evidence.get('document_id') or '采购资料'
    page = evidence.get('page')
    location = str(evidence.get('body_location') or evidence.get('location') or '')
    match = re.fullmatch(r'page:(\d+)', location)
    if match:
        location = f'第 {match.group(1)} 页'
    elif page and f'第 {page} 页' not in location:
        location = (location + ' · ' if location else '') + f'第 {page} 页'
    return f'{name} · {location}' if location else str(name)


def library_items(repo):
    items = []
    for task in repo.list_tasks():
        task_id = task['id']
        candidates = repo.list('candidates', task_id)
        documents = repo.list('documents', task_id)
        for stored in repo.list('reports', task_id):
            report = stored.get('report')
            if not isinstance(report, dict):
                continue
            history = stored.get('history_version', {})
            context = {**task, 'scenario': history.get('requirement', {}).get('scenario') or task.get('scenario', '')}
            title = task_title(context, history.get('candidates') or candidates, documents)
            baseline = (stored.get('snapshot') or {}).get('baseline') or {}
            category = str(baseline.get('category') or '').strip()
            if category:
                title = category + '选型'
            categories = [category] if category else instrument_names(title) or ['品类未识别']
            created = history.get('created_at') or report.get('created_at')
            try:
                stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
                day = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            except (AttributeError, ValueError, TypeError):
                day = None
            items.append({'id': report['report_id'], 'task_id': task_id, 'title': title,
                          'categories': categories, 'created_at': created, 'date': day,
                          'version': history.get('version_number') or report.get('baseline_version') or 1, 'scenario': context['scenario'],
                          'summary': '；'.join(' '.join([str(section.get('title') or ''), str(section.get('summary') or ''), *[str(entry.get('text') or '') for entry in section.get('items', [])]]) for section in report.get('sections', []))})
    return sorted(items, key=lambda item: (item['created_at'] or '', item['id']), reverse=True)


def export_reports(items, records, snapshot_root=None):
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        written = set()
        for index, item in enumerate(items, 1):
            record = records[item['id']]
            name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', item['title'])[:60]
            prefix = f"{index:03d}-{name}-版本{item['version']}"
            lines = [f"# {item['title']}", '', f"生成时间：{item['created_at'] or '未记录'}", '', f"采购场景：{item['scenario']}", '']
            for section in record['report'].get('sections', []):
                lines.extend([f"## {section.get('title', '报告内容')}", '', str(section.get('summary') or ''), ''])
                for entry in section.get('items', []):
                    lines.extend([f"- {entry.get('label', '')}：{entry.get('text', '')}"])
                    if entry.get('missing_reason'):
                        lines.append(f"  待补充：{entry['missing_reason']}")
                    for evidence in entry.get('evidence', []):
                        lines.append(f"  来源：{evidence_label(evidence)}")
                for evidence in section.get('evidence', []):
                    lines.append(f"来源：{evidence_label(evidence)}")
                if section.get('missing_reason'):
                    lines.extend(['', f"待补充：{section['missing_reason']}"])
                lines.append('')
            text = '\n'.join(lines)
            # 两件事分开说：``no_official_source`` 是"这次检索没拿到官方来源"，
            # ``empty_results`` 是"这次检索一条结果都没有"。以前两者翻成同一句
            # 「未找到已核实的官方来源」，导出的 md 会因此说错——见
            # 。
            text = humanize_gap_text(text)
            archive.writestr(prefix + '.md', text)
            archive.writestr(prefix + '.json', json.dumps(record, ensure_ascii=False, indent=2))
            manifest = []
            for source in record.get('snapshot', {}).get('sources', []):
                entry = {k: source.get(k) for k in ('url', 'fetched_at', 'body_status', 'source_type', 'publication_context')}
                body = source.get('body_text')
                if body:
                    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
                    member = 'evidence/' + digest + '.txt'
                    if member not in written:
                        archive.writestr(member, body)
                        written.add(member)
                    entry['text_file'] = member
                snapshot = source.get('snapshot') or {}
                digest = snapshot.get('sha256', '')
                entry['original_status'] = 'not_captured'
                if snapshot:
                    entry['original_status'] = 'missing_or_invalid'
                    # Resolve from our content store, never an arbitrary path in JSON.
                    if snapshot_root is not None and re.fullmatch(r'[0-9a-f]{64}', digest):
                        root = Path(snapshot_root).resolve()
                        path = (root / (digest + '.bin')).resolve()
                        if path.parent == root and path.is_file():
                            payload = path.read_bytes()
                            if hashlib.sha256(payload).hexdigest() == digest and len(payload) == snapshot.get('size'):
                                member = 'evidence/' + digest + '.bin'
                                if member not in written:
                                    archive.writestr(member, payload)
                                    written.add(member)
                                entry.update(original_status='included', original_file=member,
                                             sha256=digest, size=len(payload), content_type=snapshot.get('content_type'),
                                             snapshot_kind=snapshot.get('kind', 'response_bytes'))
                manifest.append(entry)
            archive.writestr(prefix + '-来源清单.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('说明.txt', '每份报告包含 Markdown、完整结构化 JSON 及来源清单。evidence 内保存已抓正文和经哈希校验的网页/附件原件；未抓取、缺失或校验失败会明确标注。旧正文不冒充原件。报告保留历史生成时的内容，价格不等同于当前报价。\n')
    return output.getvalue()
