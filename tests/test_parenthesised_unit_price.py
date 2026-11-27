# -*- coding: utf-8 -*-
"""「列名带单位、数值在冒号后」的成交单价要能绑上币种刻度。

真实来源：CCGP「峨山彝族自治县医疗与健康服务集团 2026 年第四批医疗设备采购项目
（1包、3包、4包）中标公告」——它把成交明细写成每个字段独占一行：

    名称：血型血清学离心机
    品牌：湘仪
    规格型号：XKA-2200
    数量：1
    单价(元)：17480

正文抓到了（``body_status=fetched``、``source_type=tender_document``），模型也读对了
（返回 ``amount=17480``、``amount_unit=元``、``amount_scope=unit``，引文与原文逐字一致），
但 ``validate_price`` 在绑定币种刻度那一步拒收：原来只认「数字在前、单位在后」和
「货币符号在前」两种形状，``单价(元)：17480`` 是**单位在前**，两条都不认，绑定列表为空。
于是这条真实存在的成交单价进不了 ``prices[]``，报告只说「还没有取得同型号的成交单价」。
"""
import pytest

from backend.services.procurement_rules import validate_price

URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'

BODY = (
    '峨山彝族自治县医疗与健康服务集团2026年第四批医疗设备采购项目中标公告\n'
    '公告时间 | 2026年03月25日 16:01\n'
    '名称：血型血清学离心机\n'
    '品牌：湘仪\n'
    '规格型号：XKA-2200\n'
    '数量：1\n'
    '单价(元)：17480\n'
    '五、评审专家（单一来源采购人员）名单：\n'
)
EXCERPT = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'
SOURCE = {'url': URL, 'body_status': 'fetched', 'body_text': BODY,
          'source_type': 'tender_document'}
BASELINE = {'brand': '湘仪', 'model': 'XKA-2200', 'quantity': None}


def row(**over):
    base = {'brand': '湘仪', 'model': 'XKA-2200', 'kind': 'award', 'amount': 17480,
            'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
            'currency': 'CNY', 'date': '', 'excerpt': EXCERPT}
    base.update(over)
    return base


def test_a_parenthesised_unit_price_binds_its_amount():
    """``单价(元)：17480`` 要通过，并且按可比历史价留下。"""
    quote = validate_price(row(), SOURCE, BASELINE)
    assert quote is not None
    assert quote['unit_price'] == 17480
    assert quote['currency'] == 'CNY'
    # 日期不是这条的阻塞点：validate_price 会从公告正文里现抽并按原文核实。
    assert quote['date'] == '2026-03-25'
    assert quote['date_verified'] is True
    assert quote['level'] == 'historical_configuration'
    assert 'date' not in quote['gaps']


def test_the_same_shape_is_not_bound_to_another_amount():
    """绑定要锁在「价格标签 + 括号单位 + 冒号 + 数字」上，不是随便找得到数字就算。"""
    assert validate_price(row(amount=99999), SOURCE, BASELINE) is None


def test_a_bare_number_is_not_bound_to_a_distant_currency_word():
    """只看得到「元」字、数字与它并不相邻时，仍然不算绑上币种刻度。

    ``金额+单位`` 那条要求单位在后，``货币符号`` 那条要求 ￥/¥ 在前，
    ``标签带单位`` 那条要求有价格标签——三者都不成立，就不该放过去。
    """
    body = ('某医院医疗设备采购项目中标公告 2026年03月25日\n'
            '货物名称：离心机（元）\n'
            '规格型号：XKA-2200\n'
            '数量：1\n')
    source = {**SOURCE, 'body_text': body}
    excerpt = '规格型号：XKA-2200\n数量：1'
    assert '元' in body                      # 单位字面在正文里，只是不挨着数字
    assert validate_price(row(amount=1, excerpt=excerpt), source, BASELINE) is None


@pytest.mark.parametrize('label,amount,declared,expected', [
    ('单价(元)', 17480, '元', 17480),
    ('单价（万元）', 1.5, '万元', 15000),
    ('单价（千元）', 12, '千元', 12000),
])
def test_the_declared_unit_inside_the_label_sets_the_scale(label, amount, declared, expected):
    body = f'某医院医疗设备采购项目中标公告 2026年03月25日\n规格型号：XKA-2200\n{label}：{amount}\n'
    source = {**SOURCE, 'body_text': body}
    excerpt = f'规格型号：XKA-2200\n{label}：{amount}'
    quote = validate_price(row(amount=amount, amount_unit=declared, excerpt=excerpt),
                           source, BASELINE)
    assert quote is not None, f'{label} 应被接受'
    assert quote['unit_price'] == expected


@pytest.mark.parametrize('label,declared,amount', [
    ('成交价(元)', '元', 43300),
    ('中标价（千元）', '千元', 12),
])
def test_a_total_looking_label_is_still_refused(label, declared, amount):
    """``成交价``/``中标价`` 带括号单位仍然被拒——放宽绑定判据不等于放宽这一条。

    ``scope == unit`` 时要求引文里明说这是**单价**（``单价``/``每台``/``售价``/
    ``价格``…），因为「成交价」既可能是单价也可能是一笔总额，正是整包总价混进
    单价清单的那类风险。这次只补了「单位在前」的绑定形状，这条保守判据一个字没动。
    """
    body = f'某医院医疗设备采购项目中标公告 2026年03月25日\n规格型号：XKA-2200\n{label}：{amount}\n'
    source = {**SOURCE, 'body_text': body}
    excerpt = f'规格型号：XKA-2200\n{label}：{amount}'
    assert validate_price(row(amount=amount, amount_unit=declared, excerpt=excerpt),
                          source, BASELINE) is None
