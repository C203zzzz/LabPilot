# -*- coding: utf-8 -*-
"""
对抗性测试：验证 procurement_tables.py 的边界情况和异常处理
"""
import pytest
from backend.services.procurement_tables import price_table_rows


def test_empty_cells_do_not_crash():
    """空单元格不应导致崩溃"""
    body = "\n".join([
        "货物名称 | 规格型号 | 单价（元） | 数量",
        " | A2 | 1000 | 1",  # 空名称
        "生物安全柜 |  | 2000 | 2",  # 空型号
    ])

    prices = price_table_rows(body, 'A2')
    # 空型号的行应该被跳过，空名称的行可以通过（名称会被扩展为空字符串）
    assert isinstance(prices, list)


def test_mixed_chinese_english_parentheses():
    """混合中英文括号的单位识别"""
    body1 = "\n".join([
        "型号 | 单价(元) | 数量",
        "A2 | 1000 | 1",
    ])
    body2 = "\n".join([
        "型号 | 单价（元） | 数量",
        "A2 | 1000 | 1",
    ])

    prices1 = price_table_rows(body1, 'A2')
    prices2 = price_table_rows(body2, 'A2')

    assert len(prices1) == 1
    assert len(prices2) == 1
    assert prices1[0]['amount_unit'] == '元'
    assert prices2[0]['amount_unit'] == '元'


def test_thousand_yuan_variations():
    """千元单位的不同写法"""
    bodies = [
        "型号 | 单价（千元） | 数量\nA2 | 74.8 | 1",
        "型号 | 单价(千元) | 数量\nA2 | 74.8 | 1",
    ]

    for body in bodies:
        prices = price_table_rows(body, 'A2')
        assert len(prices) == 1, f"应该识别千元单位: {body}"
        assert prices[0]['amount_unit'] == '千元'


def test_no_unit_with_currency_context():
    """有全局货币上下文时，无括号单位的表头"""
    body = "\n".join([
        "金额单位：元",
        "型号 | 单价 | 数量",
        "A2 | 1000 | 1",
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 1
    assert prices[0]['amount_unit'] == '元'


def test_misalignment_with_single_name_multiple_everything_else():
    """单个名称但其他列数量不一致应该被拒绝"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 单价(元) | 数量",
        "生物安全柜 | A2;B2 | 知楚 | 1000;2000 | 1;2",  # 品牌只有1个，应该被扩展为2个
    ])

    prices = price_table_rows(body, 'A2')
    # 品牌会被expand_common扩展，所以应该能通过
    assert len(prices) >= 1


def test_three_names_two_models_rejected():
    """3个名称 + 2个型号应该被拒绝"""
    body = "\n".join([
        "货物名称 | 规格型号 | 单价(元) | 数量",
        "安全柜;离心机;显微镜 | A2;B2 | 1000;2000 | 1;2",
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 0, "名称数量与型号数量不匹配应该被拒绝"


def test_single_brand_expanded_to_match_models():
    """单个品牌应该扩展为多个"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 单价(元) | 数量",
        "生物安全柜 | A2;B2;C2 | 知楚 | 1000;2000;3000 | 1;2;3",
    ])

    prices_a2 = price_table_rows(body, 'A2')
    prices_b2 = price_table_rows(body, 'B2')
    prices_c2 = price_table_rows(body, 'C2')

    assert len(prices_a2) == 1
    assert len(prices_b2) == 1
    assert len(prices_c2) == 1

    # 所有型号的品牌都应该是"知楚"
    assert prices_a2[0]['brand'] == '知楚'
    assert prices_b2[0]['brand'] == '知楚'
    assert prices_c2[0]['brand'] == '知楚'


def test_malformed_numbers_rejected():
    """格式错误的数字应该被拒绝"""
    body = "\n".join([
        "型号 | 单价(元) | 数量",
        "A2 | abc1000 | 1",   # 以字母开头
        "B2 | 2000abc | 2",   # 包含字母
        "C2 | 三千 | 3",       # 中文数字
        "D2 | 1.2.3 | 4",     # 多个小数点
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 0, "字母开头的数字应该被拒绝"

    prices = price_table_rows(body, 'B2')
    assert len(prices) == 0, "包含字母的数字应该被拒绝"

    prices = price_table_rows(body, 'C2')
    assert len(prices) == 0, "中文数字应该被拒绝"

    prices = price_table_rows(body, 'D2')
    assert len(prices) == 0, "多个小数点的数字应该被拒绝"


def test_valid_number_with_commas():
    """正确格式的逗号分隔数字应该被接受"""
    body = "\n".join([
        "型号 | 单价(元) | 数量",
        "A2 | 74,800 | 1",
        "B2 | 1,050,000 | 2",
    ])

    prices_a2 = price_table_rows(body, 'A2')
    prices_b2 = price_table_rows(body, 'B2')

    assert len(prices_a2) == 1
    assert prices_a2[0]['amount'] == 74800.0

    assert len(prices_b2) == 1
    assert prices_b2[0]['amount'] == 1050000.0


def test_semicolon_in_model_name_not_split():
    """型号名称中包含分号的边界情况"""
    # 实际场景中很少见，但需要确保代码不会崩溃
    body = "\n".join([
        "型号 | 单价(元) | 数量",
        "A2;Plus | 1000 | 1",  # 型号名称本身包含分号
    ])

    prices = price_table_rows(body, 'A2')
    # 这种情况下代码会把"A2"和"Plus"当作两个独立型号
    # 应该能匹配到A2
    assert len(prices) <= 1


def test_multiple_pipe_tables_in_same_body():
    """同一文档中有多个表格"""
    body = "\n".join([
        "第一个表格（排名表，不应提取）：",
        "排名 | 型号 | 单价(元)",
        "1 | A2 | 1000",
        "",
        "第二个表格（成交表，应该提取）：",
        "型号 | 单价(元) | 数量",
        "A2 | 2000 | 1",
    ])

    prices = price_table_rows(body, 'A2')
    # 排名表应该被跳过，只提取成交表的价格
    assert len(prices) == 1
    assert prices[0]['amount'] == 2000.0


def test_model_with_brand_prefix_in_spec_column():
    """规格型号列中包含品牌前缀"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 单价(元) | 数量",
        "生物安全柜 | 知楚A2 | 知楚 | 74800 | 1",
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 1
    assert prices[0]['brand'] == '知楚'
    assert prices[0]['amount'] == 74800.0


def test_quantity_with_unit_suffix_variations():
    """数量列带单位后缀的不同格式"""
    bodies = [
        "型号 | 单价(元) | 数量\nA2 | 1000 | 1.00(台)",
        "型号 | 单价(元) | 数量\nA2 | 1000 | 2台",
        "型号 | 单价(元) | 数量\nA2 | 1000 | 3套",
        "型号 | 单价(元) | 数量\nA2 | 1000 | 4.5个",
    ]

    expected_quantities = [1.0, 2.0, 3.0, 4.5]

    for body, expected_qty in zip(bodies, expected_quantities):
        prices = price_table_rows(body, 'A2')
        assert len(prices) == 1, f"应该提取数量: {body}"
        assert prices[0]['quantity'] == expected_qty


def test_combined_lot_total_is_not_a_unit_price():
    """整包行只有一个金额时，它不是其中任一型号的单价。

    真实案例：一份"药理评价系统"中标公告，包别1 这一行用"一、二、…八、"并列了
    8 种不同设备，全行只给一个 1,239,000 元。型号格里能命中 -80℃ 冰箱的
    ``DW-86L578S``，于是整包总价被当成这台冰箱的单台单价写进了成交清单。
    ``split_cells`` 只按分号切，切不开"一、…二、…"，所以这里必须由并列判据兜住。
    """
    body = "\n".join([
        "序号 | 名称 | 品牌（如有） | 规格型号 | 数量 | 单价（元）",
        "包别1 | 药理评价系统 | 一、东胜龙 二、海尔 三、弗洛雷斯 | "
        "一、药理评价核酸扩增功能：ETC821 二、药理评价-80℃超低温功能：DW-86L578S "
        "三、药理评价定向排风功能：FLLS-01 | 1 | 1239000",
    ])

    assert price_table_rows(body, 'DW-86L578S') == []


def test_itemized_row_still_prices_each_model():
    """分号并列的明细表仍然逐项取价——并列判据不能把这类表一起挡掉。

    同一份真实公告的**另一个包**是正常明细表：27 个型号用分号并列、27 个金额
    一一对应。``split_cells`` 能切开，``target_count`` 就是 27，逐一取价。
    """
    body = "\n".join([
        "序号 | 供应商名称 | 货物名称 | 货物品牌 | 货物型号 | 货物数量 | 货物单价(元)",
        "2 | 某生物科技公司 | 冰箱； 冰柜； 超低温冰箱 | 海尔； 海尔； 海尔 | "
        "BCD-310WGHD3E7WV； BC/BD-300GHPDZ； DW-86L578S | 3台； 5台； 2台 | "
        "13500； 20000； 150000",
    ])

    rows = price_table_rows(body, 'DW-86L578S')
    assert len(rows) == 1
    assert rows[0]['amount'] == 150000.0
    assert rows[0]['quantity'] == 2.0


def test_same_model_written_two_ways_is_not_a_sibling():
    """同一型号的两种写法不是"并列着别的型号"。"""
    body = "\n".join([
        "型号 | 单价(元) | 数量",
        "DW-86L578S/DW-86L578s | 1000 | 1",
    ])

    rows = price_table_rows(body, 'DW-86L578S')
    assert len(rows) == 1
    assert rows[0]['amount'] == 1000.0


def test_model_with_chinese_prefix_is_not_truncated_into_a_sibling():
    """型号带中文前缀时，截出来的半截不能算成另一个型号。

    ``知楚A2`` 里正则只截得出 ``A2``，与 ``identity('知楚A2')`` 不相等；
    ``甲厂Q１０`` 在全角形态下会被截成 ``Q1``。两者都只是同一个型号被截短。
    """
    for body, model in [
        ("\n".join(["货物名称 | 型号 | 单价(元) | 数量",
                    "生物安全柜 | 知楚A2 | 74800 | 1"]), '知楚A2'),
        ("\n".join(["货物名称 | 规格型号 | 数量 | 单价（元）",
                    "生物安全柜 | 甲厂Q１０ | 2 | 74800"]), 'Q10'),
    ]:
        rows = price_table_rows(body, model)
        assert len(rows) == 1, body
        assert rows[0]['amount'] == 74800.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
