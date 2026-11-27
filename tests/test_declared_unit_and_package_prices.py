# -*- coding: utf-8 -*-
"""单位印在别处时的成交价，以及「整包价不能当单价」。

2026-10-03 批3 灭菌器样本（HIRAYAMA HVE-50）跑出两条被 ``unparsed_amount`` 拒掉的
真实成交价：型号对、品牌对、数字也在引文里，只是**单位印在别处**。

1. 上海农林职业技术学院 2022-06-15 成交公告：一行里并列五台设备，型号
   「HVE-50、SZX7、BCD-520WDPD、MGC-350HP、Master Touch-RUV等」对金额
   「99840.50、84720.00、3600.00、21800.00、37500.00」，「元」印在表头
   「货物单价(元)」上 —— HVE-50 的价是 99,840.50 元。
2. 武汉工程大学 2025-09-15 中标公告：逐条写「名称：高压灭菌锅 … 数量：1 单价：4.8390」，
   单位在总价句「中标（成交）金额： 115.880000 (万元)」里；同一页八条明细求和
   115.88 == 中标总价，所以 4.8390 万元 = 48,390 元。

反向也锁住：黑龙江 2022-09-07 那笔 3,651,000 元的数量单位是「批」、型号列写着
「BJ-2CD等其他设备型号」，它是整包价，不能当单台成交价；并列格里取错序号
（拿冰箱的 3600.00 当 HVE-50 的价）同样要拒。
"""
import pytest

from backend.services.procurement_rules import validate_price

SHAFC_URL = 'https://www.shafc.edu.cn/2022/0616/c4085a118568/page.htm'
SHAFC_ROW = (
    '1 | 上海曼硕生物科技有限公司 | 高压灭菌锅、体式显微镜成像系统、冰箱、人工气候箱、超纯水仪等 | '
    'Hirayama，奥林巴斯、Haier、一恒、和泰等 | HVE-50、SZX7、BCD-520WDPD、MGC-350HP、Master Touch-RUV等 | '
    '1、1、1、1、1 | 99840.50、84720.00、3600.00 、21800.00 、37500.00'
)
SHAFC_BODY = (
    '上海农林职业技术学院都市园艺产业学院建设设备采购项目成交公告\n'
    '序号 供应商名称 货物名称 货物品牌 货物型号 货物数量 货物单价(元)\n'
    + SHAFC_ROW + '\n'
    '五、评审专家（单一来源采购人员）名单： 马勇、汤萌、鲍文敏\n'
    '中标（成交）金额： 61.9800000 （万元）\n'
)
SHAFC_SOURCE = {'url': SHAFC_URL, 'body_status': 'fetched', 'body_text': SHAFC_BODY,
                'source_type': 'tender_document'}
SHAFC_BASELINE = {'brand': 'HIRAYAMA', 'model': 'HVE-50', 'quantity': None}

WIT_URL = 'http://zbcg.wit.edu.cn/jgggwtcg/2126.chtml'
WIT_ITEMS = [
    ('固体粉末荧光分光光度计', '日立', 'F-4700', '23.7000'),
    ('烃类捕集器', '欧捷', 'OJ33532', '8.9500'),
    ('台式冷冻离心机', 'Eppendorf（艾本德）', '5804R', '9.9700'),
    ('层析系统', 'Cytiva', 'AKTA Go', '37.2840'),
    ('超纯水制备及智能多参数移液系统', 'Merck', 'Milli-Q EQ 7000', '11.5190'),
    ('氮吹仪', 'Organomation', 'N-EVAP 116', '5.8680'),
    ('高压灭菌锅', 'HIRAYAMA', 'HVE-50', '4.8390'),
    ('双波电穿孔系统', 'BTX', 'Gemini SC', '13.7500'),
]
WIT_BLOCK = ''.join(
    '名称：%s\n品牌（如有）：%s\n规格型号：%s\n数量：1\n单价：%s\n' % item for item in WIT_ITEMS)
WIT_BODY = (
    '武汉工程大学层析系统等设备采购项目中标公告\n'
    '四、中标（成交）信息：\n'
    '供应商名称： 中仪睿晟（武汉）科技有限公司\n'
    '中标（成交）金额： 115.880000 (万元)\n'
    + WIT_BLOCK
)
WIT_EXCERPT = '名称：高压灭菌锅\n品牌（如有）：HIRAYAMA\n规格型号：HVE-50\n数量：1\n单价：4.8390'
WIT_SOURCE = {'url': WIT_URL, 'body_status': 'fetched', 'body_text': WIT_BODY,
              'source_type': 'tender_document'}
WIT_BASELINE = {'brand': 'HIRAYAMA', 'model': 'HVE-50', 'quantity': None}

HLJ_URL = ('https://hljcg.hlj.gov.cn/gpx-bid-file/230229/gpx-bidconfirm/2022/9/7/'
           '2c9086fc82be3c040183161fe42d7e75.pdf')
HLJ_ROW = ('1-1 | 其他不另分类的物品 | 3700000 | BJ-2CD等其他设备型号 | 上海博迅等其他设备品牌 | '
           '上海博迅医疗生物仪器股份有限公司等其他制造厂商 | 上海等其他设备产地 | '
           '3,651,000.00 | 1.00 | 批 | 3,651,000.00')
HLJ_BODY = (
    '克山县马铃薯良种繁育基地三期工程建设项目检验检测仪器采购项目中标（成交）明细\n'
    '一、合同包1 （克山县马铃薯良种繁育基地三期工程建设项目检验检测仪器采购项目）\n'
    '1.2、中标（成交）总价： 3,651,000.00 元\n'
    + HLJ_ROW + '\n'
)
HLJ_SOURCE = {'url': HLJ_URL, 'body_status': 'fetched', 'body_text': HLJ_BODY,
              'source_type': 'province_award'}
HLJ_BASELINE = {'brand': '上海博迅', 'model': 'BJ-2CD', 'quantity': None}


def shafc_row(**over):
    base = {'brand': 'Hirayama', 'model': 'HVE-50', 'kind': 'award', 'amount': 99840.50,
            'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
            'currency': 'CNY', 'date': '', 'excerpt': SHAFC_ROW}
    base.update(over)
    return base


def wit_row(**over):
    base = {'brand': 'HIRAYAMA', 'model': 'HVE-50', 'kind': 'award', 'amount': 4.839,
            'amount_unit': '万元', 'amount_scope': 'unit', 'quantity': 1,
            'currency': 'CNY', 'date': '', 'excerpt': WIT_EXCERPT}
    base.update(over)
    return base


def test_a_header_unit_price_is_accepted():
    """单位「元」印在表头：99,840.50 元要能进成交价。"""
    quote = validate_price(shafc_row(), SHAFC_SOURCE, SHAFC_BASELINE)
    assert quote is not None
    assert quote['unit_price'] == 99840.50


def test_a_sibling_amount_taken_from_the_wrong_position_is_refused():
    """同一格里第 3 个数（冰箱的 3600.00）不是 HVE-50 的价。"""
    assert validate_price(shafc_row(amount=3600.00), SHAFC_SOURCE, SHAFC_BASELINE) is None


def test_a_total_declared_unit_is_accepted_when_the_detail_sums_to_it():
    """单位「万元」印在总价句：八条明细求和==总价，4.8390 万元 = 48,390 元。"""
    quote = validate_price(wit_row(), WIT_SOURCE, WIT_BASELINE)
    assert quote is not None
    assert abs(quote['unit_price'] - 48390) < 0.01


def test_a_declared_scale_is_refused_when_the_detail_does_not_add_up():
    """总价对不上账时不给换算：同一个数字不能凭一句版面单位变成万元。"""
    body = WIT_BODY.replace('115.880000 (万元)', '9,999,999.00 (万元)')
    assert validate_price(wit_row(), {**WIT_SOURCE, 'body_text': body}, WIT_BASELINE) is None


@pytest.mark.parametrize('row_over', [
    {},
    {'amount': 3651000.0, 'quantity': 1},
])
def test_a_package_total_is_never_a_unit_price(row_over):
    """「1.00 | 批」印的是整包金额，型号列还写着「等其他设备型号」。"""
    row = {'brand': '上海博迅', 'model': 'BJ-2CD', 'kind': 'award', 'amount': 3651000.0,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
           'currency': 'CNY', 'date': '2022-09-07', 'excerpt': HLJ_ROW}
    row.update(row_over)
    assert validate_price(row, HLJ_SOURCE, HLJ_BASELINE) is None
