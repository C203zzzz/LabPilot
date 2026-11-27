# -*- coding: utf-8 -*-
"""附件正文怎么喂给抽取模型：先按文书类型剔噪声，再按标题取配置窗口。

两个已实测的缺陷（证据在
``output/pipe-supervisor-20260923/site-probe/configuration-truncation.txt``）：

* ``attachment_text[:12000]`` 从**开头**砍一刀。XKA-2200 那次运行把父公告与自身附件
  拼成 127460 字，「2.1 主要配置清单（1 台标准配置）」落在第 29380 字；DW-86L578S
  那次 48788 字，「配置清单」在第 30863 字。两次都在窗口外，抽取模型从来没见过配置
  清单，``configuration`` 恒为 ``unknown``。
* 喂进去的 12000 字里大量是「投标人须知」和声明函之类的噪声。文书类型只能在抓取
  **之后**按正文判，因为附件发现（``research_sources.attachment_leads``）跑在抓取
  之前，那时只有文件名。

下面钉住三件事：窗口按标题定位、无关文书不喂、认不出来的一律保留。
"""
import json

from backend.services.procurement_research import (
    CONFIGURATION_MAXIMUM,
    configuration_regions,
    irrelevant_document,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

MODEL = 'XKA-2200'
BRAND = '湘仪'
NOTICE_URL = 'https://ggzy.example.gov.cn/award/xka-2200-notice.htm'
ATTACHMENT_URL = 'https://ggzy.example.gov.cn/attach/download?saveFileId=1'

#: 通报正文逐字取自已跑过的公告形态：型号、数量、单价各占一行。
NOTICE_BODY = (
    '中标结果公告\n'
    '项目编号：YXZC2026-G1-00082-HCZB-0013\n'
    '采购单位：峨山彝族自治县医疗与健康服务集团\n'
    '名称：血型血清学离心机\n'
    '品牌：湘仪\n'
    '规格型号：XKA-2200\n'
    '数量：1\n'
    '单价(元)：17480\n'
)

#: 附件里型号行本身不带金额，所以「值不值得调模型」由 config 之外的 region 决定；
#: 这里照真实公告的写法给一条带金额的成交明细行。
AWARD_ROW = (
    '序号 | 货物名称 | 品牌 | 型号 | 单价（元） | 数量\n'
    '1 | 血型血清学离心机 | 湘仪 | XKA-2200 | 17480.00 | 1\n'
)

#: 「2.1 主要配置清单（1 台标准配置）」逐字抄自那条真实磋商文件。
CONFIGURATION_BLOCK = (
    '2.1 主要配置清单（1 台标准配置）\n'
    '序号 名称 规格(单位) 数量\n'
    '1 血型血清学离心机整机 台 1\n'
    '2 样本架 个 4\n'
    '3 系统传感器 套 1\n'
)

#: 噪声长这样：投标人须知、评审办法，几千字都不含任何配置。
NOISE = ('第二章 投标人须知\n投标人应仔细阅读本文件。' * 900)


def _source(url, body, source_type='tender_document', parent_url=None):
    source = {'url': url, 'body_status': 'fetched', 'source_type': source_type, 'body_text': body}
    if parent_url:
        source['parent_url'] = parent_url
    return source


def _recording_client(service):
    """装一个把 user_prompt 记下来的客户端；喂了什么靠它，不靠推断。"""
    calls = []

    class Client:
        endpoint = 'test://attachment-window'
        model = 'recording'
        last_usage = None

        def analyze(self, **kwargs):
            calls.append(kwargs.get('user_prompt', ''))
            return {'prices': []}

    service.client_factory = Client
    return calls


def _prompt_payload(user_prompt):
    """``self.model`` 把 data 序列化成 user_prompt；取回这段 JSON。"""
    start = user_prompt.find('{')
    assert start >= 0, user_prompt
    return json.loads(user_prompt[start:])


# ---------------------------------------------------------------------------
# 窗口本身
# ---------------------------------------------------------------------------

def test_a_short_attachment_is_fed_whole():
    """短附件整份喂进去，和改动前一样——改的只是长附件的取法。"""
    text = AWARD_ROW
    assert configuration_regions(text, MODEL) == [{'start': 0, 'text': text}]


def test_the_configuration_block_beyond_the_old_head_window_is_kept():
    text = NOISE + CONFIGURATION_BLOCK
    assert len(text) > CONFIGURATION_MAXIMUM
    assert '主要配置清单' not in text[:CONFIGURATION_MAXIMUM]   # 旧行为：整段落在窗口外

    regions = configuration_regions(text, MODEL)
    joined = '\n'.join(region['text'] for region in regions)

    assert '主要配置清单' in joined
    assert '样本架' in joined
    assert len(joined) <= CONFIGURATION_MAXIMUM + len(regions)  # 只多出窗口之间的换行


def test_a_window_naming_the_target_model_wins_over_a_generic_one():
    """预算不够时，含目标型号的窗口先占位——它排在一堆通用标题的最后面也一样。"""
    generic = '\n'.join(f'第{index}节 技术参数\n' + f'与本型号无关的通用条款{index}。' * 300
                        for index in range(9))
    mine = '\n第三章 采购需求\n规格型号 XKA-2200 主要配置清单：整机、样本架。'
    text = generic + mine
    assert len(text) > 2 * CONFIGURATION_MAXIMUM

    regions = configuration_regions(text, MODEL)
    joined = '\n'.join(region['text'] for region in regions)

    assert 'XKA-2200' in joined, '含型号的那一段在正文最后，按位置取本来轮不到它'
    assert '样本架' in joined
    assert len(regions) < 9, '预算只够几个窗口，说明确实是挑着取的'


def test_a_table_of_contents_entry_does_not_take_a_window():
    """目录里的「第三章 采购需求 ......... 23」不是章节正文，不该占掉一个窗口。

    实测那条 47590 字的安徽招标文件，第一个「采购需求」就出现在目录页。
    """
    contents = '目 录\n第一章 投标邀请 ....................................... 3\n' \
               '第三章 采购需求 ...................................... 23\n'
    chapter = '第三章 采购需求\n一、项目概况\n二、采购清单及技术要求\n' \
              '（十、）液相色谱仪配置清单如下：二元高压液相色谱泵+柱温箱。\n'
    text = contents + '无关正文。' * 3000 + chapter
    assert len(text) > CONFIGURATION_MAXIMUM

    regions = configuration_regions(text, MODEL)
    joined = '\n'.join(region['text'] for region in regions)

    assert '配置清单如下' in joined
    assert '.......' not in joined, '目录条目不该被当成章节标题取窗口'


def test_the_window_starts_at_the_heading_not_six_hundred_characters_earlier():
    """配置内容写在标题**后面**：窗口往前退，挤掉的是后面的有效内容。

    实测那条海口公告：标题在 29380 字处，前退 600 字取到的是上一段技术要求的尾巴
    （「具备高温闪烁提示功能……最大消毒物品外径」），从标题开始取到的才是配置表。
    另一处前退 600 字直接把窗口退进了前面的目录页。
    """
    contents = '供应商须知前附表 ................ ................ -  | 6  -' * 20
    previous = ('位，方便用户灵活切换。 10. 具备高温闪烁提示功能。 11. 中心区最高温度≥825℃±50℃。'
                'GGP20240112 - 21 - 12. 最大消毒物品外径≥ Φ14mm。\n')
    config = '2.1 主要配置清单（1 台标准配置）\n序号 名称 规格(单位) 数量\n' \
             '1 全自动化学发光测定仪整机 台 1\n2 样本架 个 4\n3 无线鼠标 个 1\n'
    text = contents + '无关正文。' * 3000 + previous + config + '验收条款与商务要求。' * 3000
    assert len(text) > CONFIGURATION_MAXIMUM

    regions = configuration_regions(text, MODEL)
    joined = '\n'.join(region['text'] for region in regions)

    assert '主要配置清单' in joined
    assert '序号' in joined and '样本架' in joined, '配置表本身必须在窗口里'
    assert '高温闪烁提示功能' not in joined, '前退带进来的上一段技术要求不该占窗口'
    assert joined.count('.') / len(joined) < 0.05, '目录点线不该被带进窗口'


def test_windows_do_not_overlap_and_a_label_free_attachment_falls_back_to_the_head():
    adjacent = '配置清单' + '甲' * 4000 + '技术参数' + '乙' * 4000
    regions = configuration_regions(adjacent, MODEL)
    spans = [(region['start'], region['start'] + len(region['text'])) for region in regions]
    for (start, end), (next_start, _) in zip(spans, spans[1:]):
        assert end <= next_start, spans
    plain = '丙' * 20000
    assert configuration_regions(plain, MODEL) == [{'start': 0, 'text': plain[:CONFIGURATION_MAXIMUM]}]


# ---------------------------------------------------------------------------
# 文书类型：剔噪声，但不误伤证据
# ---------------------------------------------------------------------------

#: 每一条都逐字取自已扫过的真实附件开头（``attachment-survey.txt``，41 条）。
NOISE_HEADS = (
    '附件1 中小企业声明函(货物) 本公司(联合体)郑重声明',
    '十四、中小企业声明函 中小企业声明函（货物） 本公司（联合体）郑重声明',
    '承诺函 我方郑重承诺，所提交的全部材料真实有效',
    '医价网·价格库平台 www.120shop.cn 数据来源01 核心功能02 竞品优势03 目录 content',
    '开标记录表 项目编号',
    '资格审查记录表 项目 项目 编号',
    '专家抽取规则 项目编号: WMU-2024CS932',
    '综合得分统计表 项目编号：GGP20240112 采购单位：海口市妇幼保健院',
    '海口市公共资源交易中心新版网站常用功能操作说明',
)

EVIDENCE_HEADS = (
    # 招标文件：配置的主要来源，名字里带「示范文本」也要留
    '安徽省政府采购项目公开招标文件示范文本（货物类） 项目名称：合肥大学2025年药理实验室设备购置项目',
    '第 1 页 共 61 页 项目编号：310000000240704113047-00173620 招标文件 采购人：华东医院',
    '— 1 — 松江区医疗急救中心心电监护除颤仪 竞争性谈判文件',
    '绍兴文理学院台式离心机等设备采购项目 公 开 招 标 文 件 招标编号: 临[2021]7988号',
    # 中标明细表：表头就是型号和单价
    '序号 | 货物名称 | 品牌 | 型号 | 单价（元） | 数 量 | 总价（元）',
    '品品目目 号号 | 品品目目名名称称 | 货货物物名名称称 | 规规格格型型号号 | 品单价（元）',
    '公开招标项目中标（成交）推荐理由 项目名称：经颅磁刺激仪 中标单位：上海丰达班克',
)


def test_documents_whose_type_is_not_evidence_are_named():
    for head in NOISE_HEADS:
        assert irrelevant_document(head + '正文……'), head


def test_evidence_documents_are_never_dropped_by_type():
    for head in EVIDENCE_HEADS:
        assert not irrelevant_document(head + '正文……'), head


def test_a_declaration_word_inside_a_price_table_is_not_a_document_type():
    """``承诺函`` 三个字可能出现在一份中标明细表里，那不是文书类型。

    逐字取自已跑过的那条 585 字附件：正文开头是
    ``序号 | 货物名称 | 品牌及规格型号 | 数量 | 单价（元）``，第 348 字才是
    ``十、主要中标标的承诺函``——表格后面附了一节承诺函。按关键词判会把它整份丢掉，
    连里面那句 ``1239000.00`` 一起丢。文书类型只看开头（``DOCUMENT_HEAD_CHARS``）。
    """
    text = ('序号 | 货物名称 | 品牌及规格型号 | 数量 | 单价（元） | 备注\n'
            '1 | ▲药理评价系统 | 一、药理评价核酸扩增功能：东胜龙、ETC821 '
            '二、药理评价-80℃超低温功能：海尔、DW-86L578s 三、药理评价定向排风功能：弗洛雷斯、'
            'FLLS-01 四、药理评价高效分离、定性鉴别与定量分析功能：福立、F80 '
            '五、药理评价定位与精准微量输送功能：瑞沃德、HOYI-R+R-480 '
            '六、药理评价无菌洁净环境功能：上海力康、HFsafe-1200LC（A2）\n'
            '| 1套 | 1239000.00 | 无\n'
            '十、主要中标标的承诺函\n'
            '我单位同意中标公告中公示以下主要中标标的并承诺：投标文件中所提供的主要\n'
            '中标标的均真实有效。\n')
    assert text.find('承诺函') > 120
    assert not irrelevant_document(text)


def test_a_commitment_letter_is_a_commitment_letter_only_when_its_title_opens_the_text():
    assert irrelevant_document('承诺函\n我方郑重承诺，所提交的全部材料真实有效。\n')
    assert irrelevant_document('附件2 承诺书 本单位承诺所投产品为原厂全新正品。')


def test_an_empty_attachment_body_carries_nothing_to_feed():
    assert irrelevant_document('')
    assert irrelevant_document('   \n  ')
    assert irrelevant_document(None)


# ---------------------------------------------------------------------------
# 端到端：喂给模型的到底是什么
# ---------------------------------------------------------------------------

def test_a_declaration_is_not_fed_and_the_configuration_is(research):  # noqa: F811
    """公告自己的附件里，声明函不喂、招标文件的配置块要喂——而且它在旧窗口之外。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    notice = _source(NOTICE_URL, NOTICE_BODY + AWARD_ROW)
    declaration = _source('https://ggzy.example.gov.cn/attach/certificate.pdf',
                          '附件1 中小企业声明函(货物) 本公司郑重声明，标的名称与技术参数如下：'
                          '血型血清学离心机，技术参数见投标文件。', parent_url=NOTICE_URL)
    tender = _source(ATTACHMENT_URL, NOISE + CONFIGURATION_BLOCK, parent_url=NOTICE_URL)
    run['sources'] = [notice, declaration, tender]
    calls = _recording_client(service)

    service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(calls) == 1, '公告本身该被交给抽取模型一次'
    payload = _prompt_payload(calls[0])
    attachment = payload['attachment']
    assert '主要配置清单' in attachment and '样本架' in attachment
    assert '中小企业声明函' not in attachment
    # 旧的取法就是这份文件的前 12000 字，也就是纯噪声；现在取到的是配置那一处。
    assert not attachment.startswith('第二章 投标人须知')
    assert payload['url'] == NOTICE_URL


def test_the_notice_body_still_grounds_an_excerpt_from_a_kept_attachment(research):  # noqa: F811
    """过滤只减少喂进去的噪声，不改「哪份正文算读过」——引文校验仍然收全部附件。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    notice = _source(NOTICE_URL, NOTICE_BODY)
    declaration = _source('https://ggzy.example.gov.cn/attach/certificate.pdf',
                          '中小企业声明函（货物） 本公司参加血型血清学离心机采购活动，'
                          '规格型号 XKA-2200 单价(元)：17480。', parent_url=NOTICE_URL)

    class Client:
        endpoint = 'test://attachment-window'
        model = 'quoting'
        last_usage = None

        def analyze(self, **kwargs):
            return {'prices': [{'brand': BRAND, 'model': MODEL, 'kind': 'award', 'amount': 17480,
                                'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
                                'currency': 'CNY', 'date': '2026-03-25', 'excerpt': '规格型号 XKA-2200 单价(元)：17480'}]}

    run['sources'] = [notice, declaration]
    service.client_factory = Client

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(prices) == 1, '声明函里的原文仍然算「读过」，引文照旧可校验'
    assert prices[0]['unit_price'] == 17480
