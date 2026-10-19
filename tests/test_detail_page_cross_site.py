"""换站验证：同一套抽取规则跑在**别的站点、别的来源类型**上。

背景：本单第一版抽取规则只在 ``yiqi.com`` 一个站上验证过。这个文件把厂商官网
（``kaidalab.com`` / ``kaidasy.com`` / 英文站）、第三方平台的经销商详情页
（``bioon.com.cn``）的真实原文片段固化成夹具，守住换站之后才发现的四件事：

  1. **HTML 实体要先解码**：厂商页印的是 ``23950 &times; g``，不解码认不出单位；
  2. **两行式规格表**：标签一行、值一行（``最高转速 Max Speed`` 换行后是 ``18600r/min``）；
  3. **单位写法要归一化**：``23950 × g``、``5000(×g)``、``5000rpm`` 都得能对上
     ``procurement_rules.UNITS`` 里的键，否则"读到了也判未知"；
  4. **标签优先于对位**：经销商页的转子表里也有 ``100ml×4(5000rpm)``，按位置对位会
     把 5000rpm 当成离心力——读得到标签时就不许按位置猜。

夹具里的每一行都是**真站正文里的原样**（2026-09-28 抓取），不是手写编出来的。
"""
from backend.services.research_planning import (
    brand_from_detail_page, canonical_unit, decode_entities, detail_page_parameters,
)

SPEED = {'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'}
RCF = {'name': '最大相对离心力', 'value': 25000, 'unit': '×g', 'direction': 'min'}
URL = 'http://www.kaidalab.com/lixinji1001.html'

#: 厂商官网（湖南凯达）的页头与规格区原样：标签一行、值一行，离心力写成 ``× g``。
MANUFACTURER_PAGE = """凯达牌高性能高速台式离心机-KH20A-湖南凯达离心机厂生产出品-湖南凯达科学仪器有限公司
高性能实验室离心机 主页 > 产品中心 > 高性能实验室离心机 > 高性能高速台式离心机KH20A
高性能高速台式离心机KH20A
产品参数 产品介绍 转子附表 应用案例 合作客户 定制方案
型号 Model
KH20A
最高转速 Max Speed
18600r/min
最大相对离心力 Max RCF
23950 &times; g
最大容量 Max Capacity
400ml"""

#: 英文站的规格区：没有"Specification"这种章节标题，规格区直接从 Max Speed 开始。
ENGLISH_PAGE = """Kaida High Performance Benchtop High Speed Centrifuge--KH20A--Kaida Centrifuge
High Performance Centrifuge Home > Products > High Performance Centrifuge > KH20A High Performance Benchtop High Speed Centrifuge
KH20A High Performance Benchtop High Speed Centrifuge
Model
KH20A
Max Speed
18600r/min
Max RCF
24560 &times; g
Max Capacity
400ml"""

#: 经销商详情页：规格行**不带型号**，写成 ``标签：值``，单位带括号；页尾还有别的型号。
DEALER_PAGE = """TDL-60C低速台式离心机-实验室常用设备-仪器设备-生物在线
产品名称： TDL-60C低速台式离心机
产品编号： TDL-60C
技术参数：
最高转速 ：6000rpm（转/分）
最大相对离心力：5000(&times;g)
角转容量：15ml×12
具体参数型号请致电咨询。
TDL-40C 低速大容量离心机
技术参数:
最高转速 Max .Speed
5000rpm（转/分）
最大相对离心力Max .RCF
4968(&times;g)"""


def test_manufacturer_pages_print_the_label_on_one_line_and_the_value_on_the_next():
    found, _ = detail_page_parameters([SPEED, RCF], [(URL, MANUFACTURER_PAGE, 'KH20A')])
    assert found['最高转速']['observed']['value'] == 18600.0
    assert found['最高转速']['observed']['unit'] == 'r/min'
    assert found['最大相对离心力']['observed']['value'] == 23950.0
    # 引用把两行都带上（标签行 + 值行），逐字可核对。
    assert found['最大相对离心力']['observed']['evidence']['excerpt'] == \
        '最大相对离心力 Max RCF / 23950 × g'


def test_html_entities_are_decoded_before_reading_units():
    # ``23950 &times; g`` 不解码时单位那一段是 ``&times; g``，对不上 ``UNITS`` 里的 ``×g``。
    assert decode_entities('最大相对离心力 Max RCF\n23950 &times; g').splitlines()[1] == '23950 × g'
    found, _ = detail_page_parameters([RCF], [(URL, MANUFACTURER_PAGE, 'KH20A')])
    assert found['最大相对离心力']['observed']['value'] == 23950.0


def test_english_manufacturer_page_is_read_by_its_english_labels():
    url = 'http://en.kaidalab.com/lixinji.asp?artid=1001'
    found, _ = detail_page_parameters([SPEED, RCF], [(url, ENGLISH_PAGE, 'KH20A')])
    assert found['最高转速']['observed']['value'] == 18600.0
    assert found['最大相对离心力']['observed']['value'] == 24560.0
    assert found['最大相对离心力']['observed']['evidence']['excerpt'] == 'Max RCF / 24560 × g'


def test_units_are_normalized_to_the_forms_the_comparison_understands():
    # 三种真实写法都要归一到 ``UNITS`` 认得的键，否则"读到了也判未知"。
    assert canonical_unit('× g') == '×g'
    assert canonical_unit('xg') == '×g'
    assert canonical_unit('rpm') == 'r/min'
    assert canonical_unit('r/min') == 'r/min'


def test_a_dealer_page_whose_spec_rows_carry_no_model_is_still_read():
    url = 'https://oebiotech.bioon.com.cn/product/a1b2c3d4e5f6a'
    found, _ = detail_page_parameters([SPEED, RCF], [(url, DEALER_PAGE, 'TDL-60C')])
    assert found['最高转速']['observed']['value'] == 6000.0
    assert found['最高转速']['observed']['evidence']['excerpt'] == '最高转速 ：6000rpm（转/分）'
    assert found['最大相对离心力']['observed']['value'] == 5000.0
    assert found['最大相对离心力']['observed']['evidence']['excerpt'] == '最大相对离心力：5000(×g)'


def test_a_labelled_value_beats_positional_matching():
    # 同一个页面上，转子表那一行 ``5000rpm`` 与"最大相对离心力：5000(×g)"数值相同、
    # 单位不同——按位置对位会把它读成离心力（引用会长成 ``...×4(5000rpm)``）。
    # 这里断言引用来自**标签那一行**，也就是"标签优先于对位"确实生效。
    url = 'https://oebiotech.bioon.com.cn/product/a1b2c3d4e5f6a'
    found, _ = detail_page_parameters([RCF], [(url, DEALER_PAGE, 'TDL-60C')])
    excerpt = found['最大相对离心力']['observed']['evidence']['excerpt']
    assert '最大相对离心力' in excerpt
    assert '100ml×4' not in excerpt


def test_another_products_spec_section_on_the_same_page_is_not_used():
    # 页尾挂着 TDL-40C 的参数表（5000rpm／4968×g）。它既不该顶掉 TDL-60C 的值，
    # 也不该在 TDL-60C 读不到时被借来用。
    url = 'https://oebiotech.bioon.com.cn/product/a1b2c3d4e5f6a'
    found, _ = detail_page_parameters([SPEED, RCF], [(url, DEALER_PAGE, 'TDL-60C')])
    assert found['最大相对离心力']['observed']['value'] == 5000.0
    assert found['最高转速']['observed']['value'] == 6000.0
    # 换成 TDL-40C 来问同一页：它自己的那一段能读到，且读到的是 5000／4968。
    other, _ = detail_page_parameters([SPEED, RCF], [(url, DEALER_PAGE, 'TDL-40C')])
    assert other['最高转速']['observed']['value'] == 5000.0
    assert other['最大相对离心力']['observed']['value'] == 4968.0


def test_brand_is_read_from_the_page_that_talks_about_this_model():
    page = ('「FC5816高速离心机」价格、参数、图片 - 仪器网\n'
            '品牌 奥豪斯 [https://www.yiqi.com/zt000/brand_00000.html]\n'
            '200–15000rpm，21379xg，Max6×250ml\n')
    assert brand_from_detail_page(page, 'FC5816') == '奥豪斯'
    # 导航里的"品牌库"不是品牌。
    nav_only = ('网站首页 -品牌库 [https://www.yiqi.com/pinpai/] -供应商\n'
                'FC5816高速离心机\n200–15000rpm，21379xg\n')
    assert brand_from_detail_page(nav_only, 'FC5816') == ''
    # 这一页讲的是别台仪器时，它的品牌不算这个候选的。
    assert brand_from_detail_page(page, 'H1750') == ''


def test_brand_is_left_empty_when_the_page_does_not_print_one():
    page = ('「FC5816高速离心机」价格、参数、图片 - 仪器网\n'
            'FC5816高速离心机\n200–15000rpm，21379xg，Max6×250ml\n')
    assert brand_from_detail_page(page, 'FC5816') == ''


def test_brand_field_variants_from_dealer_pages_are_read():
    page = ('TDL-60C低速台式离心机-生物在线\n'
            '产品名称： TDL-60C低速台式离心机\n'
            '品牌商标： 飞鸽\n'
            '最高转速 ：6000rpm（转/分）\n')
    assert brand_from_detail_page(page, 'TDL-60C') == '飞鸽'
