# -*- coding: utf-8 -*-
"""列式表格行的引文：跨列拼接的引文要能定位回原文，编造的引文仍然被拒。

实测（``设计说明`` §5.2 第 2 条）：
``task-a283e5adf675`` 的 ``http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/t20260812_27118698.htm``
正文第 48 行是一张列式成交表，每一列各自用「；」串起来。抽取层读第 2 个条目时
把**不同列的同序号单元格**拼成一行：

    倒置显微镜； 徕卡； DMi1； 2台； 55000

五个片段在正文里都在，但从不连续（``倒置显微镜`` 与 ``徕卡`` 之间还隔着名称列的
其它条目），于是 ``supported()`` 的逐字子串判定失败，一条真实的 55000 元/台成交
被判 ``excerpt_not_in_source``。同一个来源、同一份正文，由表格解析器给出的引文是
**逐字整行**，一次就过——差别在"表格列拼接 vs 整行复制"，不在空白/全角半角/换行
（``supported`` 本来就折叠掉这些）。

修法（``backend/services/procurement_rules.py`` 的 ``excerpt_in_table_row``）：引文能按
``；``/``;``/``|`` 切出多段、且这些段在 ``price_table_rows`` 认得的**同一行**里
按顺序逐字出现时，返回那一行的逐字原文并替换引文。校验没有被绕过——落库的引文
反而回到了原文上；序号对齐交给后面的 ``projected`` 分支（金额/数量/品牌一致性）。
"""
import pytest

from backend.services.procurement_rules import (
    EXCERPT_NOT_IN_BODY, excerpt_in_table_row, supported, validate_price_verdict)

URL = 'http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/t20260812_27118698.htm'
#: 逐字抄自该公告正文第 47-48 行（表头与那一行数据）。
HEADER = '序号 | 供应商名称 | 货物名称 | 货物品牌 | 货物型号 | 货物数量 | 货物单价(元)'
ROW = ('1 | 北京睿辰生物医疗科技有限公司 | A2型生物安全柜； 倒置显微镜； IVF用体式显微镜； '
       '荧光倒置显微镜； 实时荧光定量PCR系统； 超速离心机； 酶标仪； 洗板机； 多功能酶标仪 | '
       '贝晶； 徕卡； 徕卡； 徕卡； 臻合智造； 贝克曼； 帝肯； 帝肯； 帝肯 | '
       'Mars1500； DMi1； Ivesta 3； DMi8； ZHFluoroX7 Pro； Optima MAX-XP； Sunrise； '
       'HydroFlex； Spark cn | 11台； 2台； 2台； 2台； 1套； 1台； 1台； 2台； 1台 | '
       '48000； 55000； 49000； 780000； 780000； 680000； 88000； 28000； 490000')
BODY = '\n'.join([
    '2026年二期开办费动物实验楼设备采购项目中标公告',
    '三、中标（成交）信息',
    '供应商名称：北京睿辰生物医疗科技有限公司',
    '中标（成交）金额：439.0000000（万元）',
    HEADER,
    ROW,
])
SOURCE = {'url': URL, 'body_status': 'fetched', 'source_type': 'tender_document',
          'publication_date': '2026-08-12', 'body_text': BODY}
BASELINE = {'brand': 'Leica', 'model': 'DMi1'}
#: 模型对这一行给出的**真实**答案（改后 run ``research-e623442a`` 的 model_results，
#: 被记成 ``excerpt_not_in_source`` 的那条）。
MEASURED_EXCERPT = '倒置显微镜； 徕卡； DMi1； 2台； 55000'
MEASURED_RAW = {'brand': '徕卡', 'model': 'DMi1', 'amount': 55000, 'amount_unit': '元',
                'amount_scope': 'unit', 'quantity': 2, 'currency': 'CNY', 'kind': 'award',
                'date': '2026-08-12', 'excerpt': MEASURED_EXCERPT}


def test_the_measured_excerpt_is_not_a_substring_of_the_body():
    """复现：逐字子串判定确实找不到它——所以不是"引文抄错"，是拼法不同。"""
    assert supported(MEASURED_EXCERPT, BODY) is False
    # 每个片段本身都在正文里：模型没有编造任何一个字
    for fragment in ('倒置显微镜', '徕卡', 'DMi1', '2台', '55000'):
        assert fragment in BODY
    assert supported(ROW, BODY) is True          # 整行复制的那种引文一次就过


def test_the_measured_excerpt_maps_back_to_the_verbatim_row():
    assert excerpt_in_table_row(MEASURED_EXCERPT, BODY, 'DMi1') == ROW


def test_the_measured_price_now_passes_and_is_stored_as_the_verbatim_row():
    quote, refusal = validate_price_verdict(dict(MEASURED_RAW), SOURCE, BASELINE)
    assert quote is not None, refusal
    assert quote['unit_price'] == 55000
    # 落库的引文是可逐字定位的原文，而不是模型那段拼接
    assert quote['excerpt'] == ROW
    assert supported(quote['excerpt'], BODY) is True


def test_an_ordinary_verbatim_excerpt_is_untouched():
    """普通逐字引文不走这条新路径，行为一字不变。"""
    raw = dict(MEASURED_RAW, excerpt=ROW)
    quote, refusal = validate_price_verdict(raw, SOURCE, BASELINE)
    assert quote is not None, refusal
    assert quote['excerpt'] == ROW


@pytest.mark.parametrize('excerpt,why', [
    ('倒置显微镜； 徕卡； DMi1； 2台； 99999', '金额是编造的：正文里没有 99999'),
    ('倒置显微镜； 徕卡； DMi1； 2台； 55000； 水浴锅', '多出一段正文没有的文字'),
    ('这批设备成交价是 55000 元', '普通句子：不是表格行形态，也没有分隔符'),
    ('55000； 徕卡', '两段都在该行，但顺序反了（徕卡在 55000 之前）'),
])
def test_a_fabricated_excerpt_is_still_refused(excerpt, why):
    """这道门仍然是防编造引文的闸门，不是被关掉。"""
    raw = dict(MEASURED_RAW, excerpt=excerpt)
    quote, refusal = validate_price_verdict(raw, SOURCE, BASELINE)
    assert quote is None, why
    assert refusal.get('reason_code') == EXCERPT_NOT_IN_BODY, refusal


@pytest.mark.parametrize('excerpt,amount', [
    ('倒置显微镜； 徕卡； DMi1； 2台', 99999),        # 省略了金额段，但自述金额与表格不一致
    ('徕卡； DMi1', 48000),                          # 第 1 项的金额配第 2 项的型号
])
def test_omitting_a_fragment_never_lets_a_wrong_amount_through(excerpt, amount):
    """省略段落可以定位，但金额由表格解析器按目标型号独立算出来核对。

    引文里少一段不等于模型可以随便报数：``projected`` 分支要求自述的金额、数量、
    单位与品牌与 ``price_table_rows`` 对**这一行、这个型号**的解析逐项相等，
    不一致就拒。
    """
    raw = dict(MEASURED_RAW, excerpt=excerpt, amount=amount)
    quote, refusal = validate_price_verdict(raw, SOURCE, BASELINE)
    assert quote is None, refusal
    assert refusal.get('reason_code') == EXCERPT_NOT_IN_BODY, refusal


def test_a_partial_but_truthful_row_citation_is_located_as_the_whole_row():
    """模型少抄了几段、但自述的金额/数量/品牌与表格解析一致时收下。

    这不是放水：落库的引文是**逐字整行**（比模型给的更完整），金额由表格解析器
    独立核对过。改前这条会被 ``excerpt_not_in_source`` 整条丢掉。
    """
    for excerpt in ('倒置显微镜； 徕卡； DMi1； 2台', '徕卡； DMi1', '倒置显微镜； 55000'):
        quote, refusal = validate_price_verdict(
            dict(MEASURED_RAW, excerpt=excerpt), SOURCE, BASELINE)
        assert quote is not None, (excerpt, refusal)
        assert quote['excerpt'] == ROW
        assert quote['unit_price'] == 55000


def test_a_cross_item_combination_is_refused():
    """跨序号拼出来的组合要拒：第 1 项的名称 + 第 2 项的金额。

    五段都在同一行里按顺序出现，所以能过"同一行"这道门；但后面的
    ``projected`` 分支拿 ``price_table_rows`` 按目标型号 ``DMi1`` 解析出的是
    ``55000``，与引文带来的 ``48000`` 不一致，于是仍被拒。
    """
    raw = dict(MEASURED_RAW, amount=48000, excerpt='A2型生物安全柜； 徕卡； DMi1； 2台； 48000')
    quote, refusal = validate_price_verdict(raw, SOURCE, BASELINE)
    assert quote is None
    assert refusal.get('reason_code') == EXCERPT_NOT_IN_BODY, refusal


def test_fragments_spread_over_two_rows_are_refused():
    """片段必须落在**同一行**：第 1 行的名称 + 第 2 行的金额不算。"""
    body = '\n'.join([
        '中标（成交）结果公告',
        '序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价(元)',
        '1 | 倒置显微镜； 荧光显微镜 | 徕卡； 蔡司 | DMi1； DMi8 | 2台； 1台 | 55000； 49000',
        '2 | 生物安全柜； 培养箱 | 贝晶； 海尔 | DMi1； XYZ1 | 1台； 3台 | 12345； 67890',
    ])
    assert excerpt_in_table_row('倒置显微镜； 12345', body, 'DMi1') is None
    assert excerpt_in_table_row('倒置显微镜； 49000', body, 'DMi1') is not None


def test_a_row_that_the_table_parser_does_not_know_is_not_a_row():
    """表格解析器认不出的文本不构成"表格行"：没有表头时这条路径不生效。"""
    body = '\n'.join(['中标（成交）结果公告',
                      '倒置显微镜； 徕卡； DMi1； 2台； 55000'])
    assert excerpt_in_table_row(MEASURED_EXCERPT, body, 'DMi1') is None
