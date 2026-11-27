"""报告里每句"有什么"的话都要能被数据核对，且与前端说的是同一件事。

五类缺陷里可机械化的那几类在这里落地（``设计说明``）：

* 空节不许承诺内容（「已列出…」「见『证据缺口』」）；
* 前端核价页列着的东西（被拒金额的原因、引文、快照指纹）导出报告里必须也有；
* 前端「其他品牌比较」那句诚实的话必须原样进报告；
* 上传资料里已经提取的厂家参数必须进报告，并注明出处是上传资料；
* 报告的"事实陈述"条目必须带出处，没有出处的只能标成"本轮要求"。
"""
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from backend.services.procurement_research import (
    EMPTY_SECTION_NOTES, NO_ALTERNATIVES_NOTE, SNAPSHOT_NOTE, official_source_count)
from backend.services.price_rejections import REJECTION_REASON_LABELS, REJECTION_REASON_NOTES
from backend.services.product_facts import FIELD_LABELS
from backend.services.report_library import export_reports
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend' / 'src'

#: 与 ``output/e2e-blind/check_report_quality.py`` 同一把尺子：那一单在真实报告里
#: 数出的内部话就是按这些指纹数的（25 处）。改前改后必须同尺。
INTERNAL_PATTERNS = {
    'Q编号': r'\bQ\d{1,3}\b',
    '代码补回': r'代码补回',
    '模块编号_M': r'\bM\d{2}\b',
    'fact_id/evidence_id': r'fact_id|evidence_id',
    '内部失败代号': r'no_official_source|tender_body_unverified|provider_failed|empty_results',
    '内部字段名': r'\b(payload|baseline_id|research_run_id|source_type|body_status|price_kind|reason_code|document_id|page_or_location|field_name)\b',
    '枚举值漏出': r'\b(eligible|ineligible|comparable_candidate|recommended_comparison|unknown|not_requested|partial)\b',
    '英文长句': r'[A-Za-z]{3,}(?:\s+[A-Za-z]{2,}){6,}',
    '异常类名': r'\b[A-Za-z]+(?:Error|Exception|Timeout)\b',
    '哈希与内部标识': r'\b[0-9a-f]{32,}\b|\bsha256\b|\bpage:\d+\b',
    '内部阶段措辞': r'检索阶段的判定|研究阶段已取得正文',
    # 只认"裸代号"。``医价网（120shop.cn）`` 这种写好的平台名 + 域名不算——域名本来
    # 就该给读者（同一份报告的来源清单里全是可点的地址）。盲测那一轮数出 2 处，两处
    # 都是裸代号（``120shop 遇到人机验证``），所以尺子对得上。
    '第三方站点标识': r'(?<![\w.])(120shop|bidcenter|chinamae)(?![\w.])',
}

#: 空节不许出现的承诺语。空节没有内容可指、也没有内容可列。
EMPTY_SECTION_PROMISES = ('已列出', '见「', '如下', '下列', '下面', '详见')


def rendered_strings(report):
    """报告里会渲染给读者的每一个字符串（= 页面上任何一层能看到的话）。

    页面上折叠几层不影响这件事：折叠只是展示方式，字符串还是这些。所以扫这一份
    就等于扫了台面与所有折叠层。
    """
    values = []
    # 总述（``headline`` / ``narrative`` / ``facts`` / ``blockers``）默认就展开在页面
    # 最上面，是"任何一层"里最显眼的一层，必须一起扫。原来这里直接把 summary 整个
    # ``str()`` 成一个 dict，虽然碰巧也能扫到里面的字，但报错位置没法读。
    keys = {'summary', 'title', 'label', 'text', 'excerpt', 'missing_reason', 'document_name',
            'headline', 'narrative', 'blockers', 'value', 'note', 'reason'}

    def walk(node, key=None):
        if isinstance(node, dict):
            for name, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, name)
                elif name in keys:
                    values.append(str(value or ''))
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and key in keys:
            values.append(node)

    walk(report)
    return values


def exported_markdown(service, report):
    """走产品自己的导出打包（报告 md + 结构化 JSON + 来源清单）。"""
    record = service.repo.get('reports', report['report_id'])
    items = [{'id': report['report_id'], 'title': '测试报告', 'version': 1, 'created_at': None, 'scenario': '用途'}]
    payload = export_reports(items, {report['report_id']: record}, ROOT / 'output' / 'nonexistent-snapshots')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(n for n in archive.namelist() if n.endswith('.md'))
        return archive.read(name).decode('utf-8')


def js_map(source, name):
    """把一个 ``const NAME = { '键': '值' }`` 字面量拆成 dict（只吃单引号字符串值）。"""
    match = re.search(r'const\s+' + name + r'\s*=\s*\{(.*?)\n\}', source, re.S)
    assert match, f'前端里找不到 {name}'
    return dict(re.findall(r"([A-Za-z_]\w*)\s*:\s*'([^']*)'", match.group(1)))


def empty_run(service, run, **overrides):
    """一轮"什么都没查到"的研究：0 成交价、0 替代候选、0 二手记录。"""
    run.update(status='partial', prices=[], republished_prices=[], alternatives=[],
               price_rejections=[], gaps=[], lanes={'price': 'completed', 'alternatives': 'completed'})
    run.update(overrides)
    service.save(run)
    return run


# ---------------------------------------------------------------------------
# ② 空节：不许说"见某处"，也不许说"已列出"
# ---------------------------------------------------------------------------

def test_an_empty_section_never_promises_content(research):  # noqa: F811
    service, run, _, _ = research
    empty_run(service, run, round=101)

    report = service.report(run['task_id'], run['id'])
    empties = [section for section in report['sections'] if not (section.get('items') or [])]

    assert empties, '这一轮本来就该有空节（否则这个测试什么也没测到）'
    for section in empties:
        summary = str(section.get('summary') or '')
        assert not any(word in summary for word in EMPTY_SECTION_PROMISES), \
            f'{section["key"]} 空着却说有内容：{summary}'
    # 五处"有引导语的空节"（盲测那一轮数出来的 5 条）必须各有一句如实说明，
    # 由 ``EMPTY_SECTION_NOTES`` 一处落地。
    for key, note in EMPTY_SECTION_NOTES.items():
        section = next((s for s in empties if s.get('key') == key), None)
        assert section is not None and section['summary'] == note, f'{key} 空节没有如实说明'


def test_the_promised_rejection_excerpts_are_actually_printed(research):  # noqa: F811
    """核价节原来写「被拒收的原文与原因见『证据缺口』」，而 12 条一条都没印。

    现在只有两条路：要么真的印出来（这一条），要么如实说本轮没有（上一条）。
    """
    service, run, _, _ = research
    record = {'url': 'https://ggzy.example/notice/9', 'model': 'Q10', 'reason_code': 'model_mismatch',
              'detail': '引文中的型号是 Q20，与本次目标型号 Q10 不一致：不能作为本次的成交价证据。',
              'excerpt': '规格型号：Q20 单价(元)：12345', 'other_models': ['Q20'],
              'snapshot_sha256': 'a' * 64}
    empty_run(service, run, round=102, price_rejections=[record])

    report = service.report(run['task_id'], run['id'])
    md = exported_markdown(service, report)

    assert '被拒收的原文与原因见' not in md, '那句话指向的「证据缺口」里并没有这些原文'
    assert record['excerpt'] in md
    # 迭代规范（设计规范）：逐条只留短信息「引文型号：X。」（型号称呼由 label 上的
    # 「型号不一致 · 本次目标型号 」承载），原来那句共同解释不再逐条重复；长句原文
    # 已归一，所以这里钉的是归一后的形状 + 该保留的事实（引文、快照说明）一条没少。
    assert '引文型号：Q20。' in md
    assert '不能作为本次的成交价证据。' in md
    assert '与本次目标型号 Q10 不一致' not in md
    assert REJECTION_REASON_LABELS['model_mismatch'] in md
    # 快照指纹本身不印（业务口径：哈希任何一层都不出现），但"快照还在、去哪核对"
    # 这句话必须与前端逐字相同。
    assert record['snapshot_sha256'] not in md
    assert SNAPSHOT_NOTE in md
    section = next(s for s in report['sections'] if s.get('key') == 'price_rejections')
    assert len(section['items']) == 1


def test_the_second_hand_section_does_not_claim_rows_it_does_not_have(research):  # noqa: F811
    service, run, _, _ = research
    empty_run(service, run, round=103)

    report = service.report(run['task_id'], run['id'])
    md = exported_markdown(service, report)
    section = next(s for s in report['sections'] if s.get('key') == 'republished')

    assert section['items'] == []
    assert '已列出采购方、日期与金额' not in md
    assert '本轮没有从聚合站转载页读到可用的二手成交记录' in md


# ---------------------------------------------------------------------------
# ④ 前端有的，导出报告必须有；两处说法必须一致
# ---------------------------------------------------------------------------

def test_the_no_alternatives_note_is_the_same_sentence_in_both_places(research):  # noqa: F811
    frontend = (FRONTEND / 'pages' / 'ResearchResults.jsx').read_text(encoding='utf-8')
    assert NO_ALTERNATIVES_NOTE in frontend, (
        '前端「其他品牌比较」那句诚实的话与报告用的常量对不上了：'
        '改前端就要一起改 backend/services/procurement_research.py 的 NO_ALTERNATIVES_NOTE')

    service, run, _, _ = research
    empty_run(service, run, round=104)
    report = service.report(run['task_id'], run['id'])

    section = next(s for s in report['sections'] if s.get('key') == 'alternatives')
    assert section['items'] == []
    assert section['summary'] == NO_ALTERNATIVES_NOTE
    assert NO_ALTERNATIVES_NOTE in exported_markdown(service, report)


def test_the_frontend_rejection_labels_are_the_backends_labels():
    """同一批被拒记录，前端与报告不能各说一套（原来前端还漏了两个代号）。"""
    source = (FRONTEND / 'pages' / 'ResearchResults.jsx').read_text(encoding='utf-8')
    labels = js_map(source, 'rejectionLabels')

    assert labels == REJECTION_REASON_LABELS, (
        '前端 rejectionLabels 与 backend/services/price_rejections.py 的 '
        'REJECTION_REASON_LABELS 不一致；认不出的代号在报告里会退成内部话')


def test_the_frontend_rejection_reason_notes_are_the_backends_notes():
    """共同解释只有一份（迭代规范）：前端分节说明与后端报告节首逐字相同。

    迭代规范 之前那句话写在每条 ``detail`` 里（实测一节重复十几次）；现在两边都在
    "一组说一次"，只要有一边改字，同一批事实就又变成两个说法。
    """
    source = (FRONTEND / 'services' / 'rejectionPlacement.js').read_text(encoding='utf-8')
    notes = js_map(source, 'REJECTION_REASON_NOTES')

    assert notes == REJECTION_REASON_NOTES, (
        '前端 rejectionPlacement.js 的 REJECTION_REASON_NOTES 与 '
        'backend/services/price_rejections.py 的不一致')


def test_the_frontend_field_labels_are_the_backends_labels():
    """资料分析的字段名在页面上叫什么，在报告里就得叫什么。"""
    source = (FRONTEND / 'pages' / 'TaskWorkspace.jsx').read_text(encoding='utf-8')
    labels = js_map(source, 'fieldLabels')

    for name, chinese in FIELD_LABELS.items():
        assert labels.get(name) == chinese, f'{name} 在页面上叫 {labels.get(name)!r}，报告里叫 {chinese!r}'


def test_the_snapshot_note_is_the_same_sentence_in_both_places():
    source = (FRONTEND / 'pages' / 'ResearchResults.jsx').read_text(encoding='utf-8')
    assert SNAPSHOT_NOTE in source, (
        '前端那条"快照已留存、去哪核对"的说明与报告用的常量对不上了：'
        '改前端就要一起改 backend/services/procurement_research.py 的 SNAPSHOT_NOTE')


# ---------------------------------------------------------------------------
# ① 上传资料里已经提取的厂家参数必须进报告
# ---------------------------------------------------------------------------

def document_analysis(fields):
    return {'kind': 'deepseek_analysis', 'analysis_id': 'analysis-1', 'created_at': '2026-01-01T00:00:00+00:00',
            'status': 'completed', 'candidates': [
                {'model': 'Q10', 'brand': '甲厂', 'status': 'pending_review', 'fields': fields}]}


def seed_document_parameters(service, run):
    fields = [
        {'field_name': 'product_identity_and_configuration', 'status': 'pending_review',
         'value': {'产品名称': '恒温培养箱', '订购配置': '标准型 230V'},
         'sources': [{'document_id': 'doc-1', 'page_or_location': 'page:1', 'excerpt': 'Model Q10'}]},
        {'field_name': 'capacity_and_throughput', 'status': 'pending_review',
         'value': {'内部容积': '2 cu.ft.', '搁板数量': '2/4'},
         'sources': [{'document_id': 'doc-1', 'page_or_location': 'page:3', 'excerpt': '内部容积 2 cu.ft.'}]},
        # 资料里没写的字段不许进报告（不许拿"unknown"当参数）。
        {'field_name': 'sample_and_format_compatibility', 'status': 'unknown', 'value': 'unknown', 'sources': []},
    ]
    service.repo.put('reports', 'analysis-1', document_analysis(fields), task_id=run['task_id'])
    service.repo.put('documents', 'doc-1', {'id': 'doc-1', 'source_name': '厂家资料.pdf'}, task_id=run['task_id'])


def test_parameters_extracted_from_the_uploaded_material_reach_the_report(research):  # noqa: F811
    service, run, _, _ = research
    seed_document_parameters(service, run)
    empty_run(service, run, round=105)

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'product_fit')
    uploaded = [item for item in section['items'] if str(item.get('label', '')).startswith('上传资料 · ')]

    assert [item['label'] for item in uploaded] == ['上传资料 · 产品型号与配置', '上传资料 · 容量与处理量']
    assert '内部容积：2 cu.ft.' in uploaded[1]['text']
    assert uploaded[1]['evidence'][0]['document_id'] == 'doc-1'
    assert uploaded[1]['evidence'][0]['page'] == 3
    assert '尚未取得本型号完整厂家参数' not in section['summary']
    assert '本任务上传的产品资料' in section['summary']

    md = exported_markdown(service, report)
    assert '来源：厂家资料.pdf · 第 3 页' in md, '上传资料的出处要写到能翻页，不能印 UUID'


# ---------------------------------------------------------------------------
# ⑤ 事实陈述必须带出处
# ---------------------------------------------------------------------------

def test_every_product_fact_item_cites_evidence(research):  # noqa: F811
    """规范项（设计规范变更）：这一节只摆资料原文，所以每条都必须带出处。

    旧口径允许没有出处的条目以「本轮要求 · X」出现——把本轮要求说出来，免得它冒充
    产品参数。现在产品事实节不再输出本轮要求（要求本身在「比较基准」一节），没有逐字
    读到的原文就不产出条目，于是**无出处条目一条都不该存在**。守卫因此变强：任何没有
    出处的条目都是回归，而不是"换个口吻就行了"。
    """
    service, run, _, _ = research
    seed_document_parameters(service, run)
    empty_run(service, run, round=106)

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'product_fit')

    assert section['items'], '上传资料的原文一条都没有进产品事实节'
    for item in section['items']:
        assert item.get('evidence'), \
            f'没有出处的条目出现在了产品事实节：{item.get("label")}｜{item.get("text")}'
    assert '只罗列' in section['summary']


# ---------------------------------------------------------------------------
# ③ 内部话：任何一层都不许出现
# ---------------------------------------------------------------------------

def test_no_internal_symbol_survives_anywhere_in_the_report(research):  # noqa: F811
    service, run, _, _ = research
    url = 'https://ggzy.example/notice/11'
    legacy = (f'tender_body_unverified: {url}'
              '（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）')
    record = {'url': 'https://ggzy.example/notice/12', 'model': 'Q20', 'reason_code': 'not_awarded_candidate',
              'detail': '这条金额属于「第二中标候选人」的投标清单，不是成交价。',
              'excerpt': '第二中标候选人 Q20 报价 12,000 元', 'snapshot_sha256': 'b' * 64}
    empty_run(service, run, round=107, price_rejections=[record],
              sources=[{'url': url, 'body_status': 'fetched', 'body_text': 'Q10', 'source_type': 'tender_document'}],
              gaps=[legacy, 'aggregator: 120shop 遇到人机验证，本轮放弃该站点',
                    '官方复核接口报错（TimeoutError），剩余线索不再查询'])

    report = service.report(run['task_id'], run['id'])
    # 本 fixture 里被采购的仪器型号正好叫 ````，它的对比型号叫 ``Q20``——这是用户
    # 要买的东西的名字，不是内部规则编号；``Q编号`` 那条正则分不出两者。
    # 真实那一单的型号是英文字母+数字，不会撞。扫之前先把型号名换成占位符。
    model_names = {run['baseline']['model'], run['baseline'].get('brand'), record['model']}
    haystack = [text for text in rendered_strings(report) + [exported_markdown(service, report)]]
    for name in sorted((n for n in model_names if n), key=len, reverse=True):
        haystack = [text.replace(name, '型号') for text in haystack]

    for name, pattern in INTERNAL_PATTERNS.items():
        hits = [(match, text[:140]) for text in haystack for match in re.findall(pattern, text)]
        assert not hits, f'{name} 还在报告里：{hits[:2]}'


def test_the_official_source_count_in_the_report_matches_the_run(research):  # noqa: F811
    """报告里"已取得 N 个官方来源"这个数必须等于按同一判据复算的结果。"""
    service, run, _, _ = research
    empty_run(service, run, round=108, sources=[
        {'url': 'https://www.ccgp.gov.cn/a', 'body_status': 'fetched', 'body_text': 'Q10', 'source_type': 'tender_document'},
        {'url': 'https://shop.example/b', 'body_status': 'fetched', 'body_text': 'Q10', 'source_type': 'marketplace'}],
        gaps=['部分检索未取得官方来源；本任务已取得 %d 个官方来源，见来源清单' % official_source_count(
            [{'url': 'https://www.ccgp.gov.cn/a', 'body_status': 'fetched', 'body_text': 'Q10', 'source_type': 'tender_document'},
             {'url': 'https://shop.example/b', 'body_status': 'fetched', 'body_text': 'Q10', 'source_type': 'marketplace'}])])

    report = service.report(run['task_id'], run['id'])
    md = exported_markdown(service, report)

    claimed = re.search(r'已取得 (\d+) 个官方来源', md)
    assert claimed and int(claimed.group(1)) == official_source_count(run['sources'])


# ---------------------------------------------------------------------------
# ③ 空着的信息要能回答"我们为它做过什么努力"
# ---------------------------------------------------------------------------

def supplier_reply(service, run, payload):
    """走产品的 ``supplier-reply`` 快路径（和界面上的"回填销售回复"同一条）。"""
    updated = service.supplier_update(run['task_id'], run['id'], payload)
    report = service.report(run['task_id'], updated['id'])
    return updated, report


def baseline_item(report, label):
    section = next(s for s in report['sections'] if s.get('key') == 'baseline')
    return next(item for item in section['items'] if item['label'] == label)


def test_answered_but_not_provided_is_not_the_same_as_never_asked(research):  # noqa: F811
    """销售明确答过「资料中未提供」的项，报告不能只写「销售尚未说明」。"""
    service, run, _, _ = research
    empty_run(service, run, round=110)

    updated, report = supplier_reply(service, run, {
        'not_provided': ['tax', 'delivery'],
        'answers': [{'question': '报修响应时间（字段 service_response）', 'answer': '资料中未提供'}]})

    assert set(updated['supplier_revision']['not_provided']) == {'tax', 'delivery', 'service_response'}
    assert baseline_item(report, '税费')['text'] == '销售已回复：资料中未提供'
    assert baseline_item(report, '交货时间')['text'] == '销售已回复：资料中未提供'
    assert '销售已回复：资料中未提供' in baseline_item(report, '报修响应时间')['text']
    # 没被问过的那一项，说的是另一件事：既不能写"问了说没有"，也不能冒充问过。
    assert baseline_item(report, '运费')['text'].startswith('本轮销售回复没有涉及这一项')
    assert not any(item['text'] == '销售尚未说明' for item in
                   next(s for s in report['sections'] if s['key'] == 'baseline')['items'])


def test_a_supplier_value_that_just_says_not_provided_does_not_become_the_value(research):  # noqa: F811
    """把「资料中未提供」当成新值写进比较基准，会把税率变成一句话。"""
    service, run, _, _ = research
    empty_run(service, run, round=111)
    before = run['baseline'].get('tax')

    updated, report = supplier_reply(service, run, {'tax': '资料中未提供。'})

    assert updated['baseline'].get('tax') == before
    assert updated['supplier_revision']['not_provided'] == ['tax']
    assert baseline_item(report, '税费')['text'] == '销售已回复：资料中未提供'


def test_a_supplier_reply_with_evidence_is_listed_field_by_field(research):  # noqa: F811
    """补了什么、依据在哪一句哪一页，报告要逐项写出来（不能只留一个空节）。"""
    service, run, _, _ = research
    empty_run(service, run, round=112)

    updated, report = supplier_reply(service, run, {
        'warranty': '资料提供延保选项（2 年至 3 年，不含磨损件）',
        'field_evidence': {'warranty': {'quote': 'Extension of the warranty from 2 to 3 years',
                                        'location': 'Page 4 / 5, SERVICES'}},
        'answers': [{'question': '请说明验收方式', 'answer': '验收方式未提供。',
                     'quote': 'Temperature fluctuation at 37 °C', 'location': 'Page 1 / 5'},
                    {'question': '请确认报价是否含税', 'answer': '未提供含税完整报价。'}]})

    section = next(s for s in report['sections'] if s.get('key') == 'supplier_revision')
    labels = [item['label'] for item in section['items']]
    texts = ' | '.join(item['text'] for item in section['items'])

    assert '本轮补充 · 保修期限' in labels
    assert 'Extension of the warranty from 2 to 3 years' in texts
    assert 'Page 4 / 5, SERVICES' in texts
    assert any(label.startswith('答复 · 请说明验收方式') for label in labels)
    assert any(label.startswith('答复 · 请确认报价是否含税') for label in labels)
    # 补进去的字段在比较基准节里也要看得出来是"这一轮补的"。
    assert baseline_item(report, '保修期限')['text'].endswith('（本轮销售回复补充）')
    assert '销售回复更新' in exported_markdown(service, report)


def test_an_unrecognised_field_is_reported_not_silently_dropped(research):  # noqa: F811
    """认不出的字段必须报错（盲测：``preferences`` 被白名单静默丢掉，接口还返回 200）。"""
    service, run, _, _ = research
    empty_run(service, run, round=113)

    with pytest.raises(ValueError) as excinfo:
        service.supplier_update(run['task_id'], run['id'], {'warrranty': '两年'})
    assert 'warrranty' in str(excinfo.value)

    # 该收的字段一个都不能丢：功能偏好也要能存进去并在报告里出现。
    updated, report = supplier_reply(service, run, {'preferences': ['支持 USB 数据导出']})
    assert updated['baseline']['preferences'] == ['支持 USB 数据导出']
    section = next(s for s in report['sections'] if s.get('key') == 'supplier_revision')
    assert any(item['label'] == '本轮补充 · 功能偏好' for item in section['items'])


def test_the_gap_section_says_what_we_did_about_them(research):  # noqa: F811
    """缺口不能只写"没查到"：同一份报告要留下"为它做过什么"的账。"""
    service, run, _, _ = research
    empty_run(service, run, round=114, gaps=['本轮未取得可核实的同型号成交单价。'],
              completed_queries=[{'query': 'a'}, {'query': 'b'}, {'query': 'c'}],
              search_attempts=[{'query': 'a'}, {'query': 'b'}],
              unavailable_sources=[{'url': 'https://a.example', 'reason': '来源限制自动访问'}])

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'gaps')
    action = next(item for item in section['items'] if item['label'] == '本轮为这些缺口做过的查询')

    assert '3 次检索' in action['text'] and '2 次检索尝试' in action['text']
    assert '1 次读取没有成功' in action['text']
    assert '本轮为这些缺口做过的查询' in exported_markdown(service, report)


#: 前端与后端共用的"内部话 → 人话"用例（前端 ``frontend/tests/publicTextParity``
#: 读的是同一个文件）。盲测那一单里 25 处内部话（异常类名 5、内部分阶段措辞 18、
#: 聚合站代号 2）全都落在这份用例覆盖的类别里。
TEXT_CASES = ROOT / 'tests' / 'fixtures' / 'internal_text_cases.json'


def _text_cases():
    return json.loads(TEXT_CASES.read_text(encoding='utf-8'))


def test_the_shared_text_cases_still_cover_every_internal_class():
    """用例文件是两端共用的钉子，被删空就等于没有检查。"""
    data = _text_cases()
    assert len(data['clean']) >= 10 and len(data['keep']) >= 2
    assert {'异常类名', '内部失败代号', '第三方站点代号', '内部分阶段措辞', '内部编号'} <= set(data['patterns'])


def test_export_path_cleans_the_same_internal_text_the_page_does():
    """导出报告那条路径过去没接上翻译层：页面是人话、导出是机器腔。

    现在后端 ``report_library.humanize_gap_text`` 与前端 ``publicText.js`` 覆盖同一批
    类别，并且对着**同一批真实用例**各跑一遍（前端那份在
    ``frontend/tests/publicTextParity.test.mjs``）。这里断言后端这一端的逐字输出。
    """
    from backend.services.report_library import humanize_gap_text

    data = _text_cases()
    failures = []
    for item in data['clean']:
        shown = humanize_gap_text(item['text'])
        if shown != item['expect']:
            failures.append('形状不一致：%s -> %s（期望 %s）' % (item['text'], shown, item['expect']))
        for name, pattern in data['patterns'].items():
            if re.search(pattern, shown):
                failures.append('%s 漏出：%s -> %s' % (name, item['text'], shown))
    assert failures == []


def test_the_cleanup_leaves_human_sentences_and_source_quotes_alone():
    """只按形状替换会啃掉来源自己说的话（``HTTP Error 403``），这条钉住不误伤。"""
    from backend.services.report_library import humanize_gap_text

    failures = []
    for item in _text_cases()['keep']:
        shown = humanize_gap_text(item['text'])
        if shown != item['text']:
            failures.append('%s：%s -> %s' % (item['why'], item['text'], shown))
    assert failures == []


def test_a_stored_gap_reaches_the_report_as_a_human_sentence(research):
    """端到端：库里存的原始缺口（内部代号 + 自注句）走到报告里必须已经是人话。

    库里那份**一个字不动**（排查的人要靠 ``tender_body_unverified`` 认出检索阶段记了
    什么），只有展示副本被翻译。
    """
    service, run, _, _ = research
    # 两条都不用 URL：报告会把"未登记来源"的地址隐去，用带地址的形态测不出净化本身。
    # 第一条是"正文确实没读到"的检索期缺口（该保留，只把代号翻成人话）；第二条挂着
    # 那句内部自注，属于"正文后来读到了"的记号，按既有口径撤掉（另有用例覆盖）。
    raw_kept = 'tender_body_unverified: 该公告页当时只读到标题'
    raw_human = '聚合站 120shop 读取失败（TimeoutError），该页正文没有拿到'
    raw_resolved = ('tender_body_unverified: 该公告页后来读到了正文'
                    '（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）')
    empty_run(service, run, round=115, gaps=[raw_kept, raw_human, raw_resolved])

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'gaps')
    shown = '\n'.join(item['text'] for item in section['items'])
    assert '采购公告正文没读到：该公告页当时只读到标题' in shown
    assert '医价网（120shop.cn）' in shown and '来源读取未完成' in shown
    assert '该公告页后来读到了正文' not in shown, '正文已读到的缺口不该再印「待核实」'
    assert 'tender_body_unverified' not in shown and 'TimeoutError' not in shown
    for name, pattern in _text_cases()['patterns'].items():
        assert not re.search(pattern, shown), '%s 还在报告里：%s' % (name, shown)
    # 导出那条路径同样是人话。
    markdown = exported_markdown(service, report)
    assert '检索阶段的判定' not in markdown and 'tender_body_unverified' not in markdown
    # 原始记录还在 run 里，可追溯（净化只作用于展示副本：报告里那句模型调用前后会
    # ``persist(run)``，所以"净化结果写回 run"就等于把库里的原始记账换成人话）。
    stored = service.get(run['task_id'], run['id'])
    stored_gaps = [str(gap) for gap in (stored.get('gaps') or [])]
    assert any('tender_body_unverified' in gap for gap in stored_gaps), stored_gaps
    assert any('120shop' in gap for gap in stored_gaps), stored_gaps


# ---------------------------------------------------------------------------
# 型号写法：同一台机器的多种写法不许静默取一种
# ---------------------------------------------------------------------------

def test_two_spellings_of_the_same_model_are_both_put_in_front_of_the_reader(research):
    """带后缀与不带后缀的写法都出现在原文里时，报告必须并列写清，不能只留一种。

    盲测实测：某型号带后缀出现 9 次、不带后缀 15 次，后缀对应"带孵育"的配置版本，
    而报告只写了其中一种。产品不替读者选——列出写法、次数、上下文，并写明正文用的是
    哪一种口径。判据（``contains_model`` / ``设计规范``）不受影响。
    """
    service, run, _, _ = research
    body = ('本机为 Q10iu Incubating Absorbance Microplate Reader，用户手册见附件\n'
            'The Q10 basic unit does not include the incubating function\n'
            'Q10 适用于常规读板\n'
            'Q10iu 的孵育模块需要单独校准\n')
    empty_run(service, run, round=116,
              gaps=[],
              sources=[{'url': 'https://a.example/manual', 'title': '厂家手册',
                        'source_type': 'manufacturer_website', 'body_status': 'fetched',
                        'body_text': body}])

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'product_fit')
    item = next((entry for entry in section['items'] if entry['label'] == '型号写法有歧义'), None)

    assert item, [entry['label'] for entry in section['items']]
    assert 'Q10iu' in item['text'] and 'Q10（' in item['text']
    assert '2 处' in item['text'] and 'Incubating' in item['text']
    assert '比较基准记录的写法「Q10」' in item['text'], item['text']
    assert item['evidence'], '歧义提示也要带出处'
    # 报告里别处照旧按判据走，不因为认出后缀就把这台机器当成别的型号。
    assert 'Q10iu' in exported_markdown(service, report)


def test_one_spelling_only_never_raises_the_ambiguity_note(research):
    """只有一种写法时不许多说一句（这条提示是个"确实有歧义"的信号，不能变成噪音）。"""
    service, run, _, _ = research
    empty_run(service, run, round=117, gaps=[],
              sources=[{'url': 'https://a.example/one', 'title': '厂家手册',
                        'source_type': 'manufacturer_website', 'body_status': 'fetched',
                        'body_text': 'Q10 用户手册\nQ10 的容量为 100 ml\n'}])

    report = service.report(run['task_id'], run['id'])
    section = next(s for s in report['sections'] if s.get('key') == 'product_fit')
    assert not [entry for entry in section['items'] if entry['label'] == '型号写法有歧义']

