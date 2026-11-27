# -*- coding: utf-8 -*-
"""同系列（同厂商不同规格）型号的识别与标注。

**这个判据的产出只许当线索，不许当成交价。** 立据在 ``设计规范``：57 条判例集、
12 个真实目标型号的实测里，按"放宽型号匹配"这条路「救回」的 23 条**没有一条是
真阳性**——全是把别家型号、同系列别档当成了目标型号。``设计规范`` §3.3 还列出
会被同一把尺子一并放开的 ``BDW-86L490 ← -Y``（1 对）、``MSC ← MSC-Advantage``
（10 对）。

所以本单**没有动任何采信判据**（``contains_model`` / ``validate_price`` 一个字
未改），只做一件事：被拒的价格如果引文里有同系列型号，在拒绝说明里指出来，
让采购员知道"同系列还有报价长这样"，而不是把那个价当成本机成交价。

判据三条同时成立（``same_model_family``）：共享前缀 ≥ 3、共享段含数字、
不是"前缀 + 数字开头"。**每条都有实测反例钉着**，见下。
"""
import pytest

from backend.services.price_rejections import REJECTION_REASON_NOTES, explain_rejection
from backend.services.procurement_rules import MODEL_MISMATCH, same_model_family

# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------

#: (左, 右, 期望, 这条钉什么)。真实数据来自批2 四份 run 的 ``price_rejections``
#: （``output/e2e-final/runs/*/evidence/16-final-run.json``）。
CASES = [
    # 真实命中：水槽目标 SSW-420-2S，引文里是博迅的 SSW-420B，单价 3,800 元
    ('SSW-420B', 'SSW-420-2S', True, '水槽实测：同系列别档'),
    # 真实命中：天平目标 AX224/E，引文里是奥豪斯的 AX224ZH
    ('AX224ZH', 'AX224/E', True, '天平实测：同系列不同后缀'),
    # 既有代码（model_match_signal）举过的形状
    ('H1850R', 'H1850', True, '粘连后缀'),
    ('SPX-150BIII', 'SPX-150B', True, '设计规范 的例子'),
    # 反例 1：共享段是纯字母 —— 同一批被拒记录里的智能储物柜型号
    ('ssw-qc-7156', 'SSW-420-2S', False, '储物柜型号，共享纯字母 ssw'),
    # 反例 2：多出来的是数字 —— Cary 6000i 对 Cary 60 是另一台机器
    ('Cary 6000i', 'Cary 60', False, '另一个数字、另一台机器'),
    # 反例 3：粘连数字（model_match_signal 的排除项）
    ('X1300A2', '1300A2', False, '粘连数字，无法区分厂商前缀'),
    # 反例 4：紧凑写法共享纯字母 —— **有意漏掉**，宁可少标不可错标
    ('BCV-3S1', 'BCV-1FD', False, '紧凑同系列，共享纯字母 bcv'),
    ('SC-15', 'SW-CJ-1FD', False, '不同系列'),
    ('SSW-420-2S', 'SSW-420-2S', False, '同一个型号不算同系列'),
    ('', 'SSW-420-2S', False, '空值不抛异常'),
    (None, 'SSW-420-2S', False, 'None 不抛异常'),
    ('ABC', 'ABCDEF', False, '短于阈值'),
]


@pytest.mark.parametrize('left,right,expected,note', CASES)
def test_same_model_family(left, right, expected, note):
    assert same_model_family(left, right) is expected, note


def test_the_relation_is_symmetric():
    """标注是对称的：左右互换结论必须一致（调用方两边都可能传）。"""
    for left, right, expected, note in CASES:
        assert same_model_family(right, left) is expected, note


# ---------------------------------------------------------------------------
# 端到端：拒绝记录里必须带着这条标注，且**仍然是被拒**
# ---------------------------------------------------------------------------

TARGET = {'brand': '上海博迅', 'model': 'SSW-420-2S'}

#: 逐字抄自批2 水槽被拒记录里那条（浙江某公告的商品表行）。
SIBLING_EXCERPT = '2-4 | A02329900 其他医疗设备 | 电热恒温水槽 | 博迅 | SSW-420B | 1.00(台) | 3,800.0000'

SIBLING_BODY = (
    '中标（成交）结果公告\n'
    '序号 | 品目编码 | 品目名称 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
    + SIBLING_EXCERPT + '\n'
)


def _source(body, **overrides):
    source = {'url': 'https://example.invalid/award/1', 'body_text': body,
              'body_status': 'fetched', 'source_type': 'tender_document',
              'snapshot': {'sha256': '0' * 64}}
    source.update(overrides)
    return source


def _row(**overrides):
    row = {'brand': '博迅', 'model': 'SSW-420B', 'kind': 'award', 'amount': 3800.0,
           'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit', 'date': None,
           'excerpt': SIBLING_EXCERPT}
    row.update(overrides)
    return row


def test_a_same_family_row_stays_rejected_but_says_why():
    """同系列的价**不进成交价**，但拒绝说明里要指出同系列型号是谁。"""
    reason = explain_rejection(_row(), _source(SIBLING_BODY), TARGET)
    assert reason['reason_code'] == MODEL_MISMATCH, reason
    assert reason['same_family_models'] == ['SSW-420B'], reason
    assert '同系列' in reason['detail']
    assert 'SSW-420B' in reason['detail']
    # 措辞必须说清它只作同系列参照，否则等于把别档价放出去了。
    # （迭代规范 文案收尾：长句收短成「该价只作同系列参照。」，「同品类的不同型号不能互相
    #  认定成交价。」那句共同解释移到分节说明，不再逐条重复。）
    assert '只作同系列参照' in reason['detail']
    assert REJECTION_REASON_NOTES[MODEL_MISMATCH] not in reason['detail']


def test_a_row_naming_an_unrelated_model_carries_no_family_annotation():
    """反例端到端：引文里是别的系列时，不许长出 ``same_family_models``。

    这条正文里的型号取自批2 水槽同一批被拒记录（森沃智能储物柜），
    它与目标 ``SSW-420-2S`` 只共享纯字母 ``ssw``。
    """
    body = ('中标（成交）结果公告\n'
            '序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
            '4 | 智能储物柜 | 森沃 | ssw-qc-7156 | 4 | 3,058.00\n')
    row = _row(brand='森沃', model='ssw-qc-7156', amount=3058.0,
               excerpt='4 | 智能储物柜 | 森沃 | ssw-qc-7156 | 4 | 3,058.00')
    reason = explain_rejection(row, _source(body), TARGET)
    assert reason['reason_code'] == MODEL_MISMATCH, reason
    assert 'same_family_models' not in reason, reason
