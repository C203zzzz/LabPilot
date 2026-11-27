# -*- coding: utf-8 -*-
"""采购要求 ≠ 产品参数：两种证据分开，绑定链条留痕。

招标文件里的「▲5.最高转速：≥4500r/min。」是采购方开出的门槛，不是
XKA-2200 的标称参数。把它并进产品配置，用户看到的就是「XKA-2200 最高转速
4500r/min」——一条凭空捏造的产品参数。

夹具复刻真实招标文件的结构：第四章采购需求下的「（一）采购设备清单」表
（包 → 设备）、「N包 技术参数要求」下的「一、/二、/三、」设备标题加单列参数表，
以及一份中标公告正文。条款文字是自拟的，结构（单列表格、▲ 前缀、序号、
末条「配置要求」、紧跟「（三）产品注册证」）与真实文件一致。
"""
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.services import tender_requirements as T

NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
DOCX_URL = 'http://download.ccgp.gov.cn/oss/download?uuid=b695c131-8d60-4109-9735-a09d7f'

#: 第 4 包三台设备。离心机 13 条，其中 3 条 ▲；条数、序号与关键条款位置照真实文件。
COAGULATION = [
    ('1', '方法学：凝固法。'),
    ('2', '检测项目：至少包含四项凝血指标。'),
    ('▲3', '单测试用血量≤20ul。'),
    ('4', '配置要求：至少包含主机1台、电源适配器1个。'),
]
THAWING = [
    ('1', '解冻时间：从常温加热到37℃≤15分钟。'),
    ('2', '循环能力≥60L/min。'),
    ('▲3', '设备使用期限≥8年。'),
]
CENTRIFUGE = [
    ('▲1', '专用转子：红血球清洗/SERO、淋巴球清洗/HLA 各一组。'),
    ('2', '微机控制，液晶显示，无刷直流电机。'),
    ('3', '控制方式：直流无刷、微机控制。'),
    ('4', '整机按质量管理体系要求生产。'),
    ('▲5', '最高转速：≥4500r/min。'),
    ('6', '最大相对离心力：≥2000×g。'),
    ('7', '转子容量：可选配（0.25～1ml）×12。'),
    ('8', '转速精度：优于或等于±30r/min。'),
    ('9', '定时范围 1min～99min。'),
    ('10', '整机噪声<60dB（A）。'),
    ('11', '电源 AC220V±22V 50/60Hz 5A。'),
    ('▲12', '设备使用期限≥8年。【需提供设备铭牌截图】'),
    ('13', '配置要求：至少包含主机1台、角转子12*7ml。'),
]


def _xml(blocks):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    parts = []
    for kind, payload in blocks:
        if kind == 'p':
            parts.append('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(payload))
        else:
            rows = ''.join('<w:tr>' + ''.join(
                '<w:tc><w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p></w:tc>' % escape(cell)
                for cell in row) + '</w:tr>' for row in payload)
            parts.append('<w:tbl>%s</w:tbl>' % rows)
    return '<w:document %s><w:body>%s</w:body></w:document>' % (ns, ''.join(parts))


def _docx(blocks):
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', _xml(blocks))
    return buffer.getvalue()


def _clauses(items):
    return [[number + '.' + text] for number, text in items]


def _tender_blocks():
    return [
        ('p', '第四章 采购需求'),
        ('p', '一、采购清单及技术参数要求'),
        ('p', '（一）采购设备清单'),
        ('t', [
            ['序号', '设备名称', '单位', '数量', '预算单价（元）', '合计（元）', '是否接受进口'],
            ['4包 预算金额：¥87600.00元'],
            ['1', '凝血全血凝固时间监测仪（核心产品）', '1', '台', '50000.00', '50000.00', '否'],
            ['2', '干式溶浆机', '1', '台', '20000.00', '20000.00', '否'],
            ['3', '血型血清学离心机', '1', '台', '17600.00', '17600.00', '否'],
        ]),
        ('p', '（二）技术参数要求'),
        ('p', '4包 技术参数要求'),
        ('p', '一、凝血全血凝固时间监测仪（核心产品）'),
        ('t', _clauses(COAGULATION)),
        ('p', '二、干式溶浆机'),
        ('t', _clauses(THAWING)),
        ('p', '三、血型血清学离心机'),
        ('t', _clauses(CENTRIFUGE)),
        ('p', '（三）产品注册证'),
        ('p', '★所投产品为医疗器械的须提供医疗器械注册证或备案凭证。'),
    ]


def _award_block(package, name, brand, model, price):
    return ['货物类', '标段名称：峨山彝族自治县第四批医疗设备采购项目 第%d包' % package,
            '名称：' + name, '品牌：' + brand, '规格型号：' + model, '数量：1',
            '单价(元)：' + str(price)]


def _notice_text():
    return '\n'.join([
        '峨山彝族自治县医疗与健康服务集团2026年第四批医疗设备采购项目中标公告',
        '四、主要标的信息',
        *_award_block(4, '凝血全血凝固时间监测仪', '广州万孚', 'OCG-102', 49800),
        *_award_block(4, '干式溶浆机', '三江', 'WGH-II', 19880),
        *_award_block(4, '血型血清学离心机', '湘仪', 'XKA-2200', 17480),
    ])


def _run(blocks=None, report=None):
    text, parsed = PublicWebSearchProvider.decode_body_with_report(
        _docx(blocks or _tender_blocks()), 'application/octet-stream', DOCX_URL)
    return {'sources': [
        {'url': NOTICE_URL, 'title': '峨山彝族自治县第四批医疗设备采购项目中标公告',
         'source_type': 'tender_document', 'body_status': 'fetched', 'body_text': _notice_text()},
        {'url': DOCX_URL, 'source_type': 'tender_document', 'body_status': 'fetched',
         'body_text': text, 'parent_url': NOTICE_URL, 'parse_report': report or parsed},
    ]}


def _device(run, package, name):
    return next(device for entry in run['tender_requirements']['packages'] if entry['package'] == package
                for device in entry['devices'] if device['name'] == name)


def test_every_clause_of_a_device_becomes_its_own_referable_row():
    run = _run()
    assert T.record(run) is True
    device = _device(run, '4包', '血型血清学离心机')
    assert [row['text'] for row in device['requirements']] == [text for _, text in CENTRIFUGE]
    assert [row['index'] for row in device['requirements']] == list(range(1, 14))
    assert [row['key'] for row in device['requirements']] == [
        number.startswith('▲') for number, _ in CENTRIFUGE]
    assert all(isinstance(row['line'], int) for row in device['requirements'])


def test_the_heading_above_a_table_keeps_its_own_rows():
    """「标题 + 下面那张表」的对应关系不能丢：否则 13 条会挂到错误的设备上。"""
    run = _run()
    T.record(run)
    centrifuge = _device(run, '4包', '血型血清学离心机')['requirements']
    coagulation = _device(run, '4包', '凝血全血凝固时间监测仪')['requirements']
    assert len(coagulation) == len(COAGULATION) and len(centrifuge) == len(CENTRIFUGE)
    assert not {row['text'] for row in coagulation} & {row['text'] for row in centrifuge}
    assert '最高转速：≥4500r/min。' in [row['text'] for row in centrifuge]
    assert _device(run, '4包', '血型血清学离心机')['requirement_source']['heading'] == '三、血型血清学离心机'


def test_the_last_clause_is_not_swallowed_by_the_next_heading():
    """末条后面紧跟「（三）产品注册证」，不能被拼进第 13 条。"""
    run = _run()
    T.record(run)
    last = _device(run, '4包', '血型血清学离心机')['requirements'][-1]
    assert last['text'] == CENTRIFUGE[-1][1]
    assert '产品注册证' not in last['text']


def test_the_binding_chain_names_every_step_and_where_it_was_read():
    run = _run()
    T.record(run)
    device = _device(run, '4包', '血型血清学离心机')
    steps = [link['step'] for link in device['binding_chain']]
    assert steps == ['采购清单', '技术参数要求', '中标公告']
    listed, requirements, award = device['binding_chain']
    assert listed['document'] == DOCX_URL and isinstance(listed['line'], int)
    assert requirements['lines'][1] - requirements['lines'][0] + 1 == len(CENTRIFUGE)
    assert award['url'] == NOTICE_URL and '湘仪' in award['evidence'] and 'XKA-2200' in award['evidence']


def test_the_award_is_read_from_the_notice_never_invented():
    run = _run()
    T.record(run)
    award = _device(run, '4包', '血型血清学离心机')['award']
    assert award['brand'] == '湘仪' and award['model'] == 'XKA-2200'
    assert award['unit_price'] == '17480' and award['package'] == '4包'
    assert award['notice_url'] == NOTICE_URL
    assert _device(run, '4包', '干式溶浆机')['award']['model'] == 'WGH-II'
    assert _device(run, '4包', '凝血全血凝固时间监测仪')['award']['model'] == 'OCG-102'


def test_the_requirement_is_a_separate_evidence_type_from_product_configuration():
    run = _run()
    T.record(run)
    stored = run['tender_requirements']
    assert stored['evidence_type'] == 'tender_requirement'
    # 等级低于产品实测参数：「中标即满足」是符合性审查推导出的制度推断。
    assert stored['evidence_level'] == 'inferred_compliance'
    assert 'configuration' not in run


def test_the_wording_says_it_is_a_requirement_not_a_product_parameter():
    run = _run()
    T.record(run)
    statement = _device(run, '4包', '血型血清学离心机')['requirements'][4]['statement']
    assert '本项目要求' in statement and '中标人须满足' in statement
    assert '不等于该型号的产品标称参数' in statement
    assert '最高转速：≥4500r/min。' in statement


def test_the_same_product_in_two_packages_is_demoted_to_a_project_level_requirement():
    """歧义不绑定：同一品类出现在多个包里，只能作为项目级要求展示。"""
    ambiguous = [
        ('p', '第四章 采购需求'), ('p', '（一）采购设备清单'),
        ('t', [['序号', '设备名称', '单位', '数量', '预算单价（元）', '合计（元）', '是否接受进口'],
               ['1包 预算金额：¥17600.00元'],
               ['1', '血型血清学离心机', '1', '台', '17600.00', '17600.00', '否'],
               ['4包 预算金额：¥17600.00元'],
               ['1', '血型血清学离心机', '1', '台', '17600.00', '17600.00', '否']]),
        ('p', '（二）技术参数要求'),
        ('p', '1包 技术参数要求'), ('p', '一、血型血清学离心机'), ('t', _clauses(CENTRIFUGE)),
        ('p', '4包 技术参数要求'), ('p', '一、血型血清学离心机'), ('t', _clauses(CENTRIFUGE)),
    ]
    run = _run(ambiguous)
    assert T.record(run) is True
    stored = run['tender_requirements']
    assert not stored['packages'] or all(
        device['name'] != '血型血清学离心机' for entry in stored['packages'] for device in entry['devices'])
    assert len(stored['project_level']) == 2
    assert '无法绑定' in stored['project_level'][0]['reason']


def test_a_quoted_price_table_in_a_docx_feeds_the_same_row_extractor_as_a_pdf():
    """docx 与 PDF 产出同一行形状，下游不需要为它养第二套解析。

    断言用的输入与 ``test_pdf_attachment_tables`` 里那份 PDF 逐格相同。
    """
    from backend.services.procurement_tables import price_table_rows
    payload = _docx([
        ('p', '中标明细'),
        ('t', [['型号', '单价(元)', '数量'], ['LA2-4A2', '48000', '2']]),
    ])
    body = PublicWebSearchProvider.decode_body(payload, 'application/octet-stream')
    assert '中标明细' in body                                   # 表格外的叙述保留
    assert '型号 | 单价(元) | 数量' in body
    assert 'LA2-4A2 | 48000 | 2' in body
    rows = price_table_rows(body, 'LA2-4A2')
    assert len(rows) == 1
    assert rows[0]['amount'] == 48000 and rows[0]['quantity'] == 2
    assert rows[0]['amount_scope'] == 'unit'


def test_the_docx_report_maps_every_table_to_the_heading_above_it():
    """行形状之外还要有结构：否则纯文本里分不清哪张表属于哪台设备。"""
    text, report = PublicWebSearchProvider.decode_body_with_report(
        _docx(_tender_blocks()), 'application/octet-stream', DOCX_URL)
    lines = text.split('\n')
    assert report['tables'] == 4                      # 采购清单表 + 三张参数表
    assert report['incomplete'] is False
    centrifuge = next(table for table in report['table_index'] if table['heading'] == '三、血型血清学离心机')
    assert centrifuge['rows'] == len(CENTRIFUGE)
    # 报告里的行号必须能用返回文本直接索引（裁掉首尾空行曾让它们差 3 行）。
    assert lines[centrifuge['heading_line']] == '三、血型血清学离心机'
    assert [lines[number] for number in range(centrifuge['start_line'], centrifuge['end_line'] + 1)] == [
        number + '.' + value for number, value in CENTRIFUGE]


def test_line_numbers_that_do_not_match_the_report_fall_back_instead_of_misbinding():
    """正文与解析报告不同源时宁可退化成行序推断，也不按错位的行号绑定。"""
    payload = _docx(_tender_blocks())
    text, report = PublicWebSearchProvider.decode_body_with_report(payload, 'application/octet-stream', DOCX_URL)
    shifted = dict(report)
    shifted['table_index'] = [dict(table, heading_line=(table.get('heading_line') or 0) + 1000)
                              for table in report['table_index']]
    run = _run(report=shifted)
    assert run['sources'][1]['body_text'] == text
    assert T.record(run) is True
    device = _device(run, '4包', '血型血清学离心机')
    assert [row['text'] for row in device['requirements']] == [value for _, value in CENTRIFUGE]
    assert device['requirement_source']['line_start'] is None       # 报告被弃用，走行序
