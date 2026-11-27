# -*- coding: utf-8 -*-
"""
测试 procurement_tables.py 的表格对齐逻辑
"""
import pytest
from backend.services.procurement_tables import label_line_rows, price_table_rows

def test_single_name_with_multiple_models():
    """测试：货物名称单值 + 型号多值的场景"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 单价(万元) | 数量",
        "生物安全柜 | A2;B2 | 知楚;知楚 | 7.48;8.5 | 1;2",
    ])

    prices_a2 = price_table_rows(body, 'A2')
    assert len(prices_a2) == 1, "应该能提取A2的价格"
    assert prices_a2[0]['amount'] == 7.48
    assert prices_a2[0]['brand'] == '知楚'

    prices_b2 = price_table_rows(body, 'B2')
    assert len(prices_b2) == 1, "应该能提取B2的价格"
    assert prices_b2[0]['amount'] == 8.5


def test_thousand_yuan_unit_recognition():
    """测试：千元单位识别"""
    body = "\n".join([
        "货物名称 | 规格型号 | 单价（千元） | 数量",
        "生物安全柜 | A2 | 74.8 | 1",
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 1, "应该能识别千元单位"
    assert prices[0]['amount'] == 74.8
    assert prices[0]['amount_unit'] == '千元'


def test_misaligned_should_be_rejected():
    """测试：不对齐的表格应该被拒绝"""
    body = "\n".join([
        "货物名称 | 规格型号 | 单价(元) | 数量",
        "A型;B型;C型 | X1;X2 | 1000;2000 | 1;2",  # 3个名称 vs 2个型号
    ])

    prices = price_table_rows(body, 'X1')
    assert len(prices) == 0, "不对齐的表格应该被拒绝"


def test_all_single_values():
    """测试：全单值场景（向后兼容）"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 单价(元) | 数量",
        "生物安全柜 | A2 | 知楚 | 74800 | 1",
    ])

    prices = price_table_rows(body, 'A2')
    assert len(prices) == 1
    assert prices[0]['amount'] == 74800.0
    assert prices[0]['brand'] == '知楚'


def test_cell_markup_cannot_split_a_record_or_hide_its_amount():
    """一条记录必须只占一个物理行，单元格内的标记不能把它拆开。

    ``price_table_rows`` 用 ``len(header.cells) == len(cells)`` 判断记录是否
    完整。如果一条记录被拆成多行，列数就对不上，**整条记录连同金额一起被静默
    丢弃**——这比报错更危险，因为界面上只会显示"本轮没有取到单价"。

    真实来源里确有这种写法：平顶山市公共资源交易中心的中标公告
    （``ggzy.pds.gov.cn/zzbgs/47391.jhtml``）的型号单元格是 ``Auto<br>stainer``。
    这里是把这个契约固定下来：``web_tables.render_table`` 每行输出
    ``' | '.join(row)``，一条记录天然只占一行；``TableReader`` 又把单元格内的
    ``<br>`` 折成空格，不产生额外换行。以后若有人改动这条链路，本用例会失败，
    而不是等到某条真实公告的金额悄悄消失。
    """
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    header = ('<tr><td>货物名称</td><td>规格型号</td><td>品牌</td>'
              '<td>数量</td><td>单价（元）</td></tr>')
    html = (f'<table>{header}<tr><td>高速冷冻离心机</td>'
            '<td>Auto<br>stainer H1850R</td><td>湘仪</td>'
            '<td>1</td><td>186000.00</td></tr></table>')

    body = PublicWebSearchProvider._clean_html(
        html, 'http://ggzy.pds.gov.cn/zzbgs/47391.jhtml')

    # 1) 每条含分隔符的记录都必须是完整的 5 列，列数不允许参差。
    rows = [line for line in body.splitlines() if '|' in line]
    assert rows, '表格几何必须被保留，否则解析层看不到表格'
    assert [len(line.split('|')) for line in rows] == [5] * len(rows), (
        f'出现被拆开的表格记录：{rows!r}')

    # 2) 金额必须能被解析层读到（这才是最终目的）。
    prices = price_table_rows(body, 'H1850R')
    assert len(prices) == 1, '单元格内标记不得导致整条带金额的记录被丢弃'
    assert prices[0]['amount'] == 186000.0
    assert prices[0]['amount_unit'] == '元'
    assert prices[0]['brand'] == '湘仪'


def test_a_genuinely_ragged_row_is_still_rejected():
    """清洗层不拆行，不代表解析层要容忍列数不符的记录。"""
    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 数量 | 单价（元）",
        "高速冷冻离心机 | H1850R | 湘仪 | 186000.00",  # 只有 4 列，缺数量列
    ])

    assert price_table_rows(body, 'H1850R') == []


def test_a_qualified_unit_in_the_heading_is_still_a_price_column():
    """「单价（元/台）」这类表头仍要认出来——但这只是识别放宽，不是采信放宽。

    别人的模板常在括号里的货币字后面再写计量口径（``单价（元/台）``、
    ``单价（元/年）``）。认不出它，整张表在 ``parse_table_header`` 第一步就被判成
    "这不是价格表"，一行都读不出来；认出来也不等于放行——单位仍旧必须写在括号里
    （``单价（不含税）`` 没有货币字，照样不是价格列），金额仍旧要过 ``validate_price``。
    """
    from backend.services.procurement_tables import (extract_unit_from_heading,
                                                      is_price_heading)

    assert is_price_heading('单价（元/台）', False) is True
    assert is_price_heading('单价（元/年）', False) is True
    assert extract_unit_from_heading('单价（元/台）', False) == '元'
    # 没有货币字就不是价格列：放宽的是"括号里还能有字"，不是"没有单位也算"。
    assert is_price_heading('单价（不含税）', False) is False

    body = "\n".join([
        "货物名称 | 规格型号 | 品牌 | 数量 | 单价（元/台）",
        "高速冷冻离心机 | H1850R | 湘仪 | 1 | 186000.00",
    ])
    prices = price_table_rows(body, 'H1850R')
    assert len(prices) == 1
    assert prices[0]['amount'] == 186000.0
    assert prices[0]['amount_unit'] == '元'


#: CCGP 中标公告「四、主要标的信息」的原样排版：一件设备一行，各字段连写、无分隔符。
#: 逐字抄自 ``evidence/off/08-run-off.json`` 的 sources 里那条 CCGP 公告
#: （http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251217_25946080.htm ，第 1674 行）。
_CCGP_MAIN_ITEMS = "\n".join([
    "安徽职业技术大学2025年食品检验检测实训室建设项目中标结果公告",
    "2025年12月17日 19:31 来源：",
    "四、 主要标的信息",
    "第 1包",
    "货物类",
    "名称：▲综合食品安全检测仪品牌：易瑞规格型号：YR-MFT803数量：2台单价：19000元/台",
    "第 2包",
    "货物类",
    "名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价：12000元/台",
    "第 3包",
    "货物类",
    "名称：▲超纯水机品牌：宏科规格型号：HK-UP-30数量：1台单价：13200元/台",
])


def _ccgp_source():
    return {
        "url": "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251217_25946080.htm",
        "body_status": "fetched",
        "source_type": "tender_document",
        "body_text": _CCGP_MAIN_ITEMS + "\n",
    }


def test_a_label_line_is_structured_and_then_accepted():
    """正例：目标型号那一行必须被抽成**可采信**价格。

    连写行是本项目现有两条记录型抽取都读不到的形状（``price_table_rows`` 要表头、
    ``_AWARD_FIELD`` 要一行一对键值），所以它靠抽取模型+品牌判据；这条用例钉的是
    "免模型的那条路也能读出来"，并且**照样要过** ``validate_price`` 的每一道判据。
    """
    from backend.services.procurement_rules import validate_price

    rows = label_line_rows(_CCGP_MAIN_ITEMS, 'H18.5B')
    assert len(rows) == 1, f'连写行没被结构化出来：{rows}'
    row = rows[0]
    assert row['model'] == 'H18.5B'
    assert row['brand'] == '上海卢湘仪'
    assert row['amount'] == 12000.0
    assert row['amount_unit'] == '元'
    assert row['amount_scope'] == 'unit'
    assert row['quantity'] == 1.0
    assert row['excerpt'] == ('名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5B'
                              '数量：1台单价：12000元/台')
    assert row['table_evidence']['layout'] == 'label_line'

    # 目标品牌写「卢湘仪」（厂商简称），引文写「上海卢湘仪」（登记名）——同一家。
    assert validate_price({**row, 'kind': 'award'}, _ccgp_source(),
                          {'model': 'H18.5B', 'brand': '卢湘仪'}) is not None


def test_the_other_models_in_the_same_notice_stay_refused():
    """反例：同公告里别的型号的行，用 H18.5B 的目标去判，必须仍旧被拒。"""
    from backend.services.procurement_rules import MODEL_MISMATCH, validate_price

    for other, amount in (('YR-MFT803', 19000.0), ('HK-UP-30', 13200.0)):
        rows = label_line_rows(_CCGP_MAIN_ITEMS, other)
        assert len(rows) == 1, f'{other} 的行应能被结构化，好把型号判据单独隔离出来测'
        assert rows[0]['amount'] == amount
        refusals = {}
        assert validate_price({**rows[0], 'kind': 'award'}, _ccgp_source(),
                              {'model': 'H18.5B', 'brand': '卢湘仪'}, refusals) is None
        assert refusals.get('reason_code') == MODEL_MISMATCH

    # 正文里根本没有的型号，一行都不该产出。
    assert label_line_rows(_CCGP_MAIN_ITEMS, 'G21000') == []
    assert label_line_rows(_CCGP_MAIN_ITEMS, 'TG12X') == []


def test_a_longer_model_name_is_not_the_target_model():
    """边界：``H18.5BR`` 不是 ``H18.5B``——型号必须逐字一致，后缀不许当同一个型号。"""
    from backend.services.procurement_rules import MODEL_MISMATCH, validate_price

    body = ("名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5BR数量：1台"
            "单价：14500元/台")
    # 方向一：目标 H18.5B 时，``H18.5BR`` 那一行连结构化都不该发生（行级门）。
    assert label_line_rows(body, 'H18.5B') == []
    # 方向二：正文写 H18.5B、目标是更长的 H18.5BR 时，同样不认。
    short = ("名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台"
             "单价：12000元/台")
    assert label_line_rows(short, 'H18.5BR') == []
    # 型号判据本身也钉一遍：把 H18.5BR 的行硬喂进去，必须被 model_mismatch 拒。
    refusals = {}
    row = {'brand': '上海卢湘仪', 'model': 'H18.5BR', 'kind': 'award', 'amount': 14500.0,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'excerpt': body}
    source = {**_ccgp_source(), 'body_text': '中标结果公告\n' + body + '\n'}
    assert validate_price(row, source, {'model': 'H18.5B', 'brand': '卢湘仪'},
                          refusals) is None
    assert refusals.get('reason_code') == MODEL_MISMATCH


def test_a_line_holding_two_devices_is_skipped_whole():
    """边界：一行里连写**两件**设备 → 整行跳过，不按第一件读。

    按标签取值只能取到第一件，而 ``excerpt`` 是整行：交出去以后人看到的是两条设备
    一行，分不清哪一笔钱属于哪一件；"名称/品牌"取第一件、"单价"取第二件时更会把
    B 的价配到 A 的型号上。项目对并列型号的既有立场是"宁可少一条"，这里同理。
    两件**分行**印的正常排版不受影响（见上一个用例）。
    """
    two = ("名称：▲台式高速离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台"
           "单价：12000元/台名称：▲超纯水机品牌：宏科规格型号：HK-UP-30数量：1台"
           "单价：13200元/台")
    assert label_line_rows(two, 'H18.5B') == []
    assert label_line_rows(two, 'HK-UP-30') == []

    # 型号格里并列多个型号、整行只有一个金额：那是整包价，不是其中任一型号的单价。
    sibling = ("名称：离心机品牌：上海卢湘仪规格型号：H18.5B;H18.5BR数量：2台"
               "单价：24000元")
    assert label_line_rows(sibling, 'H18.5B') == []


def test_the_amount_scale_must_be_written_in_the_line_itself():
    """边界：金额口径只从这一行的原文读，不从别处借、不猜。"""
    rows = label_line_rows(
        # 两处都没单位：口径不明 → 不收
        "名称：离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价：12000\n"
        # 标签声明了单位（``validate_price`` 明确支持的形状）→ 收
        "名称：离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价（元）：17480\n"
        # 值自带单位 → 收
        "名称：离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价：12000元/台\n"
        # 标签与值互相矛盾（万元 vs 元）→ 不收
        "名称：离心机品牌：上海卢湘仪规格型号：H18.5B数量：1台单价（万元）：1.2元",
        'H18.5B')
    assert [(r['amount'], r['amount_unit']) for r in rows] == [(17480.0, '元'),
                                                               (12000.0, '元')]


def test_a_prose_line_without_a_model_label_is_not_a_label_line():
    """边界：行里**没有**型号标签的散文，不算"标签连写行"。

    实测来源：把这条新通道接进 ``prices()`` 之后，
    ``生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元`` 这种散文行一度被判成结构化
    价格（型号拿目标型号回退、品牌格空 → 一路全过），
    ``tests/test_alternative_price_lookup.py`` 的三个台账用例因此从
    ``read_without_price`` 翻成 ``priced``。行里"哪一格是型号"没有标签指明，这条
    通道就不该认它——``price_table_rows`` 与抽取模型才是读它的地方。
    """
    body = "安徽省本级 成交公告\n生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元"
    assert label_line_rows(body, 'Q20') == []
    # 加上型号标签之后，同一行才属于这条通道。
    labelled = "安徽省本级 成交公告\n名称：生物安全柜品牌：乙厂规格型号：Q20数量：1台单价：56,000.00元"
    rows = label_line_rows(labelled, 'Q20')
    assert len(rows) == 1
    assert rows[0]['amount'] == 56000.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
