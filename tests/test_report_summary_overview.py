# -*- coding: utf-8 -*-
"""报告总述：结构骨架机械取数，文字交给模型，模型不可用时如实降级。

用户的原话抱怨是「最后的分析报告 也没有一段简要的报告生成」。报告正文在前端默认
**折叠**，列表项在折叠状态下只显示一句「保留 N 条未完成事项或来源说明」，所以总述必须
放在 ``report()`` 返回值的**顶层** ``summary`` 键上，前端不展开就能读到，并且要给出
结构化三样（``headline`` / ``facts`` / ``blockers``）供出图，不能只给一段自然语言。

这里的每一段文字都由 mock 的模型给出：调用次数、prompt 原文、失败与降级都靠它，不靠推断。
"""
import json
from pathlib import Path

import pytest

import backend.services.procurement_research as research_module
from backend.services.procurement_research import (
    OVERVIEW_FORBIDDEN_MARKERS,
    REPORT_OVERVIEW_PROMPT,
    _human_gap,
    failure_kind_text,
    gap_action_summary,
    report_blockers,
    report_overview_facts,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

HEADLINE = '甲厂 Q10 查到两条同型号历史成交价，配置口径没有核实，还不能合并成一个区间。'
NARRATIVE = '这次查的是甲厂 Q10 容量检测用途。查到两条同型号历史成交记录，但两条公告都没写清配置，' \
            '所以这两个价格不能直接当成同一个口径比。下一步请销售补一份含税、运费和保修的完整报价。'

PRICE_ONE = {
    'model': 'Q10', 'brand': '甲厂', 'kind': 'award', 'level': 'historical_configuration',
    'comparison_level': 'historical_configuration', 'unit_price': 17480.0, 'currency': 'CNY',
    'date': '2026-03-25', 'date_verified': True, 'supplier': '乙公司', 'quantity': 1,
    'url': 'https://ggzy.example.gov.cn/award/1', 'gaps': ['configuration'],
    'configuration': 'unknown', 'excerpt': '规格型号：Q10 单价(元)：17480',
}
PRICE_TWO = {
    'model': 'Q10', 'brand': '甲厂', 'kind': 'contract', 'level': 'historical_configuration',
    'comparison_level': 'historical_configuration', 'unit_price': 19800.0, 'currency': 'CNY',
    'date': '2026-04-08', 'date_verified': True, 'supplier': '丙公司', 'quantity': 2,
    'url': 'https://ggzy.example.gov.cn/award/2', 'gaps': ['configuration'],
    'configuration': 'unknown', 'excerpt': '型号 Q10 合同单价 19800.00 元',
}


class RecordingClient:
    """把 system_prompt 记下来的模型；返回值由测试给。"""

    endpoint = 'test://report-summary'
    model = 'recording'
    last_usage = None

    def __init__(self, result=None, error=None, calls=None):
        self.result = result if result is not None else {'headline': HEADLINE, 'narrative': NARRATIVE}
        self.error = error
        self.calls = calls if calls is not None else []

    def analyze(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _prepared(research, round_number, **changes):  # noqa: F811
    service, run, _, _ = research
    run.update(status='partial', round=round_number, prices=[dict(PRICE_ONE), dict(PRICE_TWO)],
               gaps=[], alternatives=[], republished_prices=[],
               lanes={'price': 'completed', 'alternatives': 'completed'})
    run.update(changes)
    run['baseline'] = {**run['baseline'], 'supplier_price': 20000}
    run.pop('price_decision', None)   # 夹具那轮是空价格的判断，这里必须重算
    service.save(run)
    return service, run


# ---------------------------------------------------------------------------
# 一、总述字段
# ---------------------------------------------------------------------------

def test_the_report_carries_a_top_level_summary(research):  # noqa: F811
    service, run = _prepared(research, 91)
    calls = []
    service.client_factory = lambda: RecordingClient(calls=calls)

    report = service.report(run['task_id'], run['id'])
    summary = report['summary']

    assert 'summary' in report, '总述必须在顶层，前端折叠时也要读得到'
    assert not any(section.get('key') == 'summary' for section in report['sections']), '不要塞进 sections'
    assert summary['headline'] == HEADLINE
    assert summary['narrative'] == NARRATIVE
    assert summary['narrative_status'] == 'model'
    assert summary['narrative_note'] == ''
    assert len(calls) == 1, '一份报告只多花一次模型调用'
    public = {fact['label']: fact['value'] for fact in summary['facts']}
    assert public['比较基准'] == '甲厂 Q10'
    assert public['可采信的同型号成交记录'] == '2 条'
    assert public['成交供应商'] == '乙公司、丙公司'
    assert public['成交日期'] == '2026-03-25 至 2026-04-08'
    assert summary['blockers'] == []
    assert json.loads(calls[0]['user_prompt'])['facts']


def test_gaps_become_blockers_in_chinese(research):  # noqa: F811
    service, run = _prepared(research, 92, gaps=['empty_results',
                                                 'tender_body_unverified: https://ggzy.example.gov.cn/x',
                                                 'provider_failed:deepseek_web: 超时',
                                                 '销售尚未说明配置。'])
    service.client_factory = lambda: RecordingClient()

    summary = service.report(run['task_id'], run['id'])['summary']

    assert 'empty_results' not in json.dumps(summary, ensure_ascii=False)
    assert '这一次检索没有返回任何结果' in summary['blockers']
    assert '采购公告正文没读到：https://ggzy.example.gov.cn/x' in summary['blockers']
    assert '检索服务未完成：超时' in summary['blockers']
    assert '销售尚未说明配置。' in summary['blockers']


def test_an_empty_fact_is_left_out_instead_of_printed_as_unknown(research):  # noqa: F811
    service, run = _prepared(research, 93)
    run['baseline'].update(scenario='unknown', brand='')
    run['prices'] = [dict(PRICE_ONE, supplier='')]
    service.save(run)
    service.client_factory = lambda: RecordingClient()

    facts = service.report(run['task_id'], run['id'])['summary']['facts']
    labels = [fact['label'] for fact in facts]
    values = [fact['value'] for fact in facts]

    assert '采购用途' not in labels, "场景是 unknown 就不要这一条"
    assert all(value and value.lower() != 'unknown' for value in values)
    assert '乙公司' not in values


def test_facts_and_blockers_carry_no_internal_field_names(research):  # noqa: F811
    """这些字会原样印在页面上：英文枚举和 snake_case 键名一个都不许有。"""
    service, run = _prepared(research, 94, gaps=['empty_results', 'no_official_source'])
    service.client_factory = lambda: RecordingClient()

    summary = service.report(run['task_id'], run['id'])['summary']
    printed = json.dumps({'facts': summary['facts'], 'blockers': summary['blockers']}, ensure_ascii=False)

    for token in ('unknown', 'award', 'contract', 'tender_document', 'evidence_grade', 'amount_scope',
                  'source_type', 'comparison_level', 'secondary', 'empty_results', 'no_official_source'):
        assert token not in printed, token
    assert not any('_' in fact['label'] for fact in summary['facts'])


# ---------------------------------------------------------------------------
# 二、抓取失败的代号：源头写成中文，历史数据靠词典兜底
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code,expected', [
    ('empty_body', '来源正文为空'),
    ('empty_shell', '页面只有空壳'),
    ('dns_not_found', '域名解析失败'),
    ('not_found', '来源已不存在'),
    ('RemoteDisconnected', '连接被对方中断'),
    ('parse_incomplete', '正文解析不完整'),
    ('page_too_large', '页面过大'),
    ('unsupported_source_url', '这个地址不是可直接读取的页面'),
    ('http_404', '来源返回错误状态（HTTP 404）'),
    ('http_503', '来源返回错误状态（HTTP 503）'),
    ('tls_handshake_timeout', '安全连接失败'),
    # 兜底：failure_kind 的最后一档是 type(cause).__name__，异常类名是开放集合
    ('SomeUndocumentedError', '读取失败'),
    ('', '读取失败'),
])
def test_a_failure_code_is_written_in_chinese(code, expected):
    assert failure_kind_text(code) == expected


def test_a_dns_failure_is_not_eaten_by_the_shorter_not_found_rule():
    """``dns_not_found`` 里含 ``not_found``：短的先跑就会啃掉半截。"""
    text = _human_gap('该来源连续两次无法读取（dns_not_found），其他来源可正常访问')
    assert '域名解析失败' in text
    assert 'not_found' not in text
    assert '已不存在' not in text


def test_an_empty_prefix_rule_does_not_eat_the_other_empty_codes():
    """``empty_`` 开头的三个代号各有各的说法，短的不能啃掉长的。

    ``empty_results``（这次检索一条结果都没有）与 ``no_official_source``
    （有结果但没有官方类型）是两件事，说法也必须不同：它们以前同为
    「未找到已核实的官方来源」，一份报告会因此自相矛盾。
    """
    text = _human_gap('（empty_results）（empty_shell）（empty_body）')
    assert text == '（这一次检索没有返回任何结果）（页面只有空壳）（来源正文为空）'
    assert _human_gap('no_official_source') == '这一次检索未取得官方来源'


def test_a_stored_report_s_code_is_translated_when_read_back(research):  # noqa: F811
    """已经存下来的 368 个 run 里的代号改不掉了：读历史报告时靠词典翻译。"""
    service, run = _prepared(research, 96, gaps=['empty_body', 'empty_shell', 'RemoteDisconnected',
                                                 'dns_not_found', 'not_found', 'parse_incomplete'])
    service.client_factory = lambda: RecordingClient()

    blockers = service.report(run['task_id'], run['id'])['summary']['blockers']
    printed = json.dumps(blockers, ensure_ascii=False)

    assert blockers == ['来源正文为空', '页面只有空壳', '连接被对方中断',
                        '域名解析失败', '来源已不存在', '正文解析不完整']
    for code in ('empty_body', 'empty_shell', 'RemoteDisconnected', 'not_found', 'parse_incomplete'):
        assert code not in printed, code


def test_the_failure_reason_is_built_from_chinese_words(research):  # noqa: F811
    """源头那一行：``error_reason`` 不再拼 ``source['failure_kind']`` 代号。"""
    service, run, _, _ = research
    run['round'] = 97
    run['gaps'] = []
    run['sources'] = [{'url': 'http://ztb.example.edu.cn/gonggao.htm', 'body_status': 'failed',
                       'failure_kind': 'dns_not_found'},
                      {'url': 'https://a.example.gov.cn/one', 'body_status': 'fetched'},
                      {'url': 'https://b.example.gov.cn/two', 'body_status': 'fetched'}]
    service.fetch_source = lambda run, source: {**source, 'body_status': 'failed'}

    service.recover_sources(run)

    gap = run['gaps'][0]
    assert '该来源连续两次无法读取（域名解析失败）' in gap
    assert 'dns_not_found' not in gap
    assert run['unavailable_sources'][0]['reason'] == \
        '该来源连续两次无法读取（域名解析失败），其他来源可正常访问；保留URL及原始失败原因待补查'


def test_the_overview_call_is_counted_in_a_timed_stage(research):  # noqa: F811
    """一轮研究要求每一笔付费调用都落在某个计时阶段里，报告的这次也不例外。"""
    service, run = _prepared(research, 98)
    service.client_factory = lambda: RecordingClient()

    service.report(run['task_id'], run['id'])
    stored = service.get(run['task_id'], run['id'])
    stages = stored['stage_timings']

    overview = [entry for entry in stages if entry['name'] == '报告总述']
    assert overview and overview[-1]['calls']['model'] == 1
    # 两条车道同时跑时（``lanes_execution == 'together'``），重叠阶段会把同一笔调用各记
    # 一次，所以阶段之和只会 **不小于** 真实用量；只有串行那一轮才要求逐笔对上。
    # 这一处的口径与 ``test_research_stage_timings`` 里那条说明是同一套。
    total = sum(entry['calls']['model'] for entry in stages)
    if stored.get('lanes_execution') == 'together':
        assert total >= stored['usage']['model']
    else:
        assert total == stored['usage']['model']


def test_no_gap_text_is_built_from_an_exception_class_name():
    """缺口是给用户看的：三处曾经把 ``type(exc).__name__`` 拼进去。

    技术细节（异常类名、出错行）改存 ``run['failures']``，那一份不渲染到页面。
    这条是源码级守卫——两个调用点都在需要网络的分支里，跑不到就靠它挡回退。
    """
    facade = Path(research_module.__file__)
    research_files = [facade, *sorted((facade.parent / 'research').glob('*.py'))]
    source = '\n'.join(path.read_text(encoding='utf-8') for path in research_files)
    offenders = [line.strip() for line in source.splitlines()
                 if "run['gaps'].append(" in line and '__name__' in line]
    assert offenders == []
    assert "run.setdefault('failures', []).append" in source


def test_a_gap_text_never_shows_an_exception_class_name(research):  # noqa: F811
    """真跑一次 ``note_failure``：缺口是中文，类名进 ``failures``。"""
    service, run, _, _ = research
    run['gaps'] = []
    run.pop('failures', None)

    try:
        raise TimeoutError('读超时')
    except TimeoutError as error:
        service.record_failure(run, '附件地址渲染', error)

    assert run['gaps'] == ['附件地址渲染：处理数据时发生异常，已有结果已保留']
    assert run['failures'][0]['type'] == 'TimeoutError'
    assert run['failures'][0]['code'] == 'processing_error'
    assert run['failures'][0]['location']['function'] == 'test_a_gap_text_never_shows_an_exception_class_name'


# ---------------------------------------------------------------------------
# 三、prompt：去 AI 腔 + 只用中文
# ---------------------------------------------------------------------------

def test_the_prompt_carries_every_writing_ban(research):  # noqa: F811
    service, run = _prepared(research, 95)
    calls = []
    service.client_factory = lambda: RecordingClient(calls=calls)
    service.report(run['task_id'], run['id'])

    prompt = calls[0]['system_prompt']

    for ban in ('在当今', '随着……不断发展', '值得注意的是', '需要指出的是', '毋庸置疑',
                '标志着重要一步', '奠定坚实基础', '发挥关键作用', '彰显重要意义',
                '业内普遍认为', '专家指出', '有研究表明', '不少用户反馈',
                '从而确保', '进而体现', '进一步彰显', '反映了更深层次的',
                '未来可期', '迈出了重要一步', '开启新的篇章', '尽管面临诸多挑战',
                '核心问题是', '关键区别在于', '原因很简单', '综上所述',
                '不是……而是', '这不仅是', '真正重要的是', '从更大的角度看',
                '通常', '一般来说', 'snake_case', '英文内部字段名', '英文枚举值'):
        assert ban in prompt, ban
    # 事实边界与用途也必须在 prompt 里，否则模型会去补数据里没有的东西
    for rule in ('只能用给出的数据说话', '不许把推断写成事实', '结论先行', '下一步'):
        assert rule in prompt, rule
    for forbidden in ('.codex', 'SKILL.md', 'oil-tone'):
        assert forbidden not in prompt, '文风规范只作参考，不能把它的路径写进产品'


def test_the_markers_that_reject_model_text_are_the_prompt_bans():
    """prompt 写了的禁令，代码也要真的挡——两处不能各说各话。"""
    for marker in ('在当今', '值得注意的是', '未来可期', '综上所述', '这不仅是'):
        assert marker in REPORT_OVERVIEW_PROMPT
        assert marker in OVERVIEW_FORBIDDEN_MARKERS


# ---------------------------------------------------------------------------
# 三、降级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('round_number,result,note', [
    (300, {}, '没有按要求给出总述'),
    (301, {'headline': HEADLINE}, '没有按要求给出总述'),
    (302, {'headline': '', 'narrative': NARRATIVE}, '没有按要求给出总述'),
    (303, ['不是字典'], '没有按要求给出总述'),
    (304, {'headline': '在当今医疗设备采购的背景下，这次比价很有意义。', 'narrative': NARRATIVE}, '写法要求'),
    (305, {'headline': HEADLINE, 'narrative': '配置口径为 unknown，比价依据是 historical_comparable。'}, '写法要求'),
    (306, {'headline': HEADLINE, 'narrative': '这条记录的 evidence_grade 是 secondary。'}, '写法要求'),
])
def test_model_text_that_breaks_the_rules_is_dropped_not_shipped(research, round_number, result, note):  # noqa: F811
    service, run = _prepared(research, round_number)
    service.client_factory = lambda: RecordingClient(result=result)

    summary = service.report(run['task_id'], run['id'])['summary']

    assert summary['narrative_status'] == 'unavailable'
    assert summary['narrative'] == ''
    assert note in summary['narrative_note']
    # 骨架仍然是机械算出来的那一份，不受模型文字影响
    assert '甲厂 Q10' in summary['headline']
    assert {fact['label'] for fact in summary['facts']} >= {'比较基准', '可采信的同型号成交记录'}


def test_a_broken_model_call_does_not_take_the_report_down_with_it(research):  # noqa: F811
    """``report()`` 在 ``_execute`` 末尾也被调用，抛出去整份报告就没了。"""
    service, run = _prepared(research, 200)
    service.client_factory = lambda: RecordingClient(error=RuntimeError('provider down'))

    report = service.report(run['task_id'], run['id'])
    summary = report['summary']

    assert report['report_id'], '报告本身必须照常生成'
    assert summary['narrative_status'] == 'unavailable'
    assert '模型暂时不可用' in summary['narrative_note']
    assert summary['headline'] == ('甲厂 Q10：查到 2 条可采信的同型号历史成交记录，'
                                   '配置口径未核实，不能合并成一个价格区间。')
    assert {fact['label'] for fact in summary['facts']} >= {'比较基准', '比价依据'}


def test_no_price_found_says_so_instead_of_a_model_sentence(research):  # noqa: F811
    service, run = _prepared(research, 201, prices=[])
    service.client_factory = lambda: RecordingClient(error=RuntimeError('provider down'))

    summary = service.report(run['task_id'], run['id'])['summary']

    assert summary['headline'] == '甲厂 Q10：本轮没有查到可采信的同型号成交记录，暂不能判断销售报价。'
    assert dict((fact['label'], fact['value']) for fact in summary['facts'])['可采信的同型号成交记录'] == '0 条'


def test_the_model_budget_is_not_spent_beyond_its_limit(research):  # noqa: F811
    service, run = _prepared(research, 202)
    run['usage']['model'] = run['limits']['model']
    service.save(run)
    calls = []

    def forbidden():
        raise AssertionError('额度已经用完，不该再调模型')

    service.client_factory = forbidden
    summary = service.report(run['task_id'], run['id'])['summary']

    assert calls == []
    assert summary['narrative_status'] == 'unavailable'
    assert '额度已用完' in summary['narrative_note']
    assert service.get(run['task_id'], run['id'])['usage']['model'] == run['limits']['model']


def test_reading_a_saved_report_does_not_call_the_model_again(research):  # noqa: F811
    service, run = _prepared(research, 203)
    calls = []
    service.client_factory = lambda: RecordingClient(calls=calls)
    first = service.report(run['task_id'], run['id'])

    def forbidden():
        raise AssertionError('已保存的报告不该再调模型')

    service.client_factory = forbidden
    second = service.report(run['task_id'], run['id'])

    assert len(calls) == 1
    assert second['summary'] == first['summary']


def test_a_report_saved_before_the_summary_existed_gets_one_backfilled(research):  # noqa: F811
    """旧报告里没有总述字段；读到时补一次并写回，之后不再重复调用。"""
    service, run = _prepared(research, 204)
    calls = []
    service.client_factory = lambda: RecordingClient(calls=calls)
    stored = service.report(run['task_id'], run['id'])
    # 装作这是改动前存下来的报告：整份都在，只是没有顶层 summary
    service.repo.put('reports', stored['report_id'],
                     {**service.repo.get('reports', stored['report_id']),
                      'report': {k: v for k, v in stored.items() if k != 'summary'}},
                     task_id=run['task_id'])

    service.client_factory = lambda: RecordingClient(calls=calls)
    backfilled = service.report(run['task_id'], run['id'])
    again = service.report(run['task_id'], run['id'])

    assert backfilled['summary']['headline'] == stored['summary']['headline']
    assert again['summary'] == backfilled['summary']
    assert service.repo.get('reports', stored['report_id'])['report']['summary'] == backfilled['summary'], \
        '补出来的总述要写回去，否则每读一次都要补'
    assert len(calls) == 1, '模型结果本来就有缓存，补写不该再产生一次模型调用'


# ---------------------------------------------------------------------------
# 四、报告里不再有两节说同一句话
# ---------------------------------------------------------------------------

def test_the_price_section_and_the_decision_section_no_longer_repeat(research):  # noqa: F811
    service, run = _prepared(research, 205)
    service.client_factory = lambda: RecordingClient()

    report = service.report(run['task_id'], run['id'])
    sections = {section['key']: section for section in report['sections']}

    assert sections['decision']['summary'] == report['sections'][0]['summary']
    assert sections['prices']['summary'] != sections['decision']['summary'], '同一句话不该印两遍'
    assert '2 条同型号成交记录' in sections['prices']['summary']
    assert '2026-03-25 至 2026-04-08' in sections['prices']['summary']


def test_the_summary_blockers_and_the_gap_section_say_the_same_thing(research):  # noqa: F811
    """同一批缺口在两处必须渲染成同一批话（缺口节那条"做过什么"的账除外）。

    这两处原来各走各的取舍：总述的 ``report_blockers`` 只看 ``run['gaps']``，而
    「证据缺口」节先 ``reconcile_tender_body_gaps`` —— 正文已经读到的
    ``tender_body_unverified`` 缺口、以及带着旧版阶段注记的那一条，缺口节都不再印。
    同一条缺口，总述里说"待核实"、缺口节里已经撤掉，读者不知道信哪个。

    **两处的差别只允许有一种**：「证据缺口」节多一条"本轮一共查了几次、几次没读成"
    的账（``gap_action_summary``）——那是内容差异，有意保留，不在这条用例的比对范围里。
    """
    service, run = _prepared(research, 206)
    service.client_factory = lambda: RecordingClient()
    # 造一条正文已读到的来源：它的 tender_body_unverified 缺口两边都不该印
    fetched = dict((run.get('sources') or [{}])[0])
    fetched.update({'url': 'https://example.gov.cn/read', 'body_status': 'fetched', 'body_text': '正文'})
    run['sources'] = list(run.get('sources') or []) + [fetched]
    run['gaps'] = [
        'tender_body_unverified: https://example.gov.cn/read',
        'tender_body_unverified: https://example.gov.cn/never',
        '价格核查：公告及附件未提供该项（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）',
        'no_official_source',
        '价格核查：接口没有答复，本轮不再重试。',
    ]
    service.save(run)

    report = service.report(run['task_id'], run['id'])

    blockers = list(report['summary']['blockers'])
    gaps_section = next(section for section in report['sections'] if section['key'] == 'gaps')
    action = gap_action_summary(run)
    shown = [item['text'] for item in gaps_section['items']
             if not (action and item.get('text') == action['text'])]

    assert shown == blockers, '总述与证据缺口节对同一批缺口的说法必须一致'
    # 取舍本身也钉住：正文已经读到的那条，两处都不印；没读到的那条两处都留着
    assert not any('example.gov.cn/read' in text for text in shown)
    assert any('example.gov.cn/never' in text for text in shown)
    assert not any('检索阶段的判定' in text for text in shown), '旧版阶段注记两处都不许再印'


def test_the_mechanical_helpers_are_pure_functions(research):  # noqa: F811
    """facts / blockers 不依赖 service，也不调用模型——出图那部分不能有网络副作用。"""
    service, run = _prepared(research, 206)
    facts = report_overview_facts(run, {'state': 'historical_comparable'})
    assert [fact['label'] for fact in facts][:2] == ['比较基准', '采购用途']
    assert report_blockers({'gaps': ['empty_results']}) == ['这一次检索没有返回任何结果']
    assert report_blockers({'gaps': ['no_official_source']}) == ['这一次检索未取得官方来源']
    assert report_blockers({}) == []
