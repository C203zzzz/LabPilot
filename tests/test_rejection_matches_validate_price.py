"""The stated reason for a refusal must be the predicate that actually fired.

Why this file exists
--------------------
``explain_rejection`` used to run its own ordered list of predicates, separate
from the acceptance gate. The consequence was measured twice in one round:

* the CCGP ``单价(元)：17480`` row: ``validate_price`` returned a quote for it
  (the date is read out of the announcement body) while the report explained it
  as "no verifiable transaction date" -- a predicate that never executes, because
  an ungrounded date only lowers the comparability level and is recorded as a
  gap;
* ``tests/test_price_rejections.py``'s bare ``35,200.00`` fixture: the row stops
  at the currency-binding predicate (there is no 元 and no ￥ anywhere in the
  citation) while the explanation said the same thing about the date.

The root cause was two sources of truth for one decision. The reason codes now
live in ``procurement_rules`` next to the gate; these tests pin the two together.
"""
import pytest

from backend.services.price_rejections import (
    AMOUNT_NOT_UNIT_PRICE,
    BRAND_MISMATCH,
    BUDGET_NOT_ACHIEVED,
    DATE_UNGROUNDED,
    MODEL_MISMATCH,
    NOT_AWARD_EVIDENCE,
    UNKNOWN,
    UNPARSEABLE_AMOUNT,
    explain_rejection,
)
from backend.services.procurement_rules import validate_price

TARGET = {'brand': '湘仪', 'model': 'H1850R'}

QDU_BODY = (
    '采购预算 | ￥35,000.00 | 成交金额 | ￥32,980.00\n'
    '采购清单\n'
    '采购内容 | 品牌及型号 | 预算单价（元） | 采购数量 | 分项报价\n'
    '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00\n'
    '售后服务 | 整机质保一年\n'
)

#: (name, row, body). Every row here is a shape that occurs in the frozen
#: acceptance runs or in the pinned fixtures; none is invented geometry.
CASES = [
    (
        'citation_without_any_currency_marker',
        {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 35200.0,
         'amount_unit': '元', 'quantity': 2.0, 'amount_scope': 'unit', 'date': '',
         'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00'},
        '中标结果公告 高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00\n',
    ),
    (
        'budget_instead_of_an_achieved_price',
        {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 35000.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
         'excerpt': '台式高速冷冻离心机 | 湘仪/H1850R | 预算单价（元）35000 | 1'},
        '成交结果公告\n台式高速冷冻离心机 | 湘仪/H1850R | 预算单价（元）35000 | 1\n',
    ),
    (
        'package_total_scope',
        {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 35200.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'package_total',
         'date': '2022-12-16',
         'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | ￥35,200.00'},
        '中标结果公告 高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | ￥35,200.00\n',
    ),
    (
        'sibling_model',
        {'brand': '中科中佳', 'model': 'HC-3016R', 'kind': 'award', 'amount': 32980.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
         'excerpt': '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00'},
        QDU_BODY,
    ),
    (
        'same_model_other_brand',
        {'brand': '中科中佳', 'model': 'H1850R', 'kind': 'award', 'amount': 32980.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
         'excerpt': '台式高速冷冻离心机 | 中科中佳/H1850R | 35000 | 1 | ￥32,980.00'},
        '成交结果公告\n台式高速冷冻离心机 | 中科中佳/H1850R | 35000 | 1 | ￥32,980.00\n',
    ),
    (
        'page_without_award_wording',
        {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 1.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
         'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | 面议'},
        '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | 面议\n',
    ),
]

EXPECTED_CODES = {
    'citation_without_any_currency_marker': UNPARSEABLE_AMOUNT,
    'budget_instead_of_an_achieved_price': BUDGET_NOT_ACHIEVED,
    'package_total_scope': AMOUNT_NOT_UNIT_PRICE,
    'sibling_model': MODEL_MISMATCH,
    'same_model_other_brand': BRAND_MISMATCH,
    'page_without_award_wording': NOT_AWARD_EVIDENCE,
}


def _source(body, **overrides):
    source = {'url': 'https://cg.qdu.edu.cn/jjjg/9920.chtml', 'body_text': body,
              'body_status': 'fetched', 'source_type': 'tender_document'}
    source.update(overrides)
    return source


@pytest.mark.parametrize('name,row,body', CASES, ids=[case[0] for case in CASES])
def test_the_reason_code_is_the_predicate_that_fired(name, row, body):
    source = _source(body)
    refusals = {}

    assert validate_price(dict(row), source, TARGET, refusals) is None, name
    assert refusals.get('reason_code') == EXPECTED_CODES[name], name

    # With the gate's own verdict handed in ...
    assert explain_rejection(dict(row), source, TARGET, refusals=refusals)['reason_code'] \
        == refusals['reason_code']
    # ... and without it: the explainer must run the gate, not a private list.
    assert explain_rejection(dict(row), source, TARGET)['reason_code'] \
        == refusals['reason_code']


def test_a_missing_date_is_never_reported_as_the_refusal_reason():
    """An ungrounded date is a gap, not a refusal.

    ``validate_price`` records ``date`` in ``gaps`` and lowers the level to
    ``model_reference``; it never refuses on it. Reporting it as the cause told
    the reader to go find a date, which would not have admitted the row either.
    """
    for name, row, body in CASES:
        reason = explain_rejection(dict(row), _source(body), TARGET)
        assert reason['reason_code'] != DATE_UNGROUNDED, name


def test_a_row_the_gate_accepts_is_never_given_a_named_refusal():
    """The measured 17480 shape: the gate returns a quote, so no reason applies."""
    body = ('中标结果公告\n'
            '名称：血型血清学离心机 品牌：湘仪 规格型号：H1850R 数量：1 单价(元)：17480\n')
    row = {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 17480,
           'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
           'excerpt': '品牌：湘仪 规格型号：H1850R 数量：1 单价(元)：17480', 'date': ''}
    source = _source(body)

    assert validate_price(dict(row), source, TARGET) is not None

    reason = explain_rejection(dict(row), source, TARGET)

    assert reason['reason_code'] == UNKNOWN
    assert reason['reason_code'] != DATE_UNGROUNDED
    assert '通过' in reason['detail']


def test_the_explainer_cannot_promote_an_amount():
    """Whatever the reason, the explanation is display-only: no unit price leaks."""
    for name, row, body in CASES:
        reason = explain_rejection(dict(row), _source(body), TARGET)
        assert 'unit_price' not in reason, name
        assert reason['detail'].strip(), name
