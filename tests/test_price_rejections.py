"""Why-was-this-price-rejected explanations must be truthful and display-only.

A correct refusal is indistinguishable from "the system found nothing" unless
the refusal is explained. These tests pin the explanation for the case the
acceptance round actually hit -- 青岛大学 published ￥32,980.00 for a
``中科中佳/HC-3016R``, which is correctly NOT an ``H1850R`` price -- and pin the
boundary that an explanation never becomes a price row.
"""
import pytest

from backend.services.price_rejections import (
    AMOUNT_NOT_UNIT_PRICE,
    BODY_UNAVAILABLE,
    BRAND_MISMATCH,
    BUDGET_NOT_ACHIEVED,
    DATE_UNGROUNDED,
    EXCERPT_NOT_IN_BODY,
    MODEL_MISMATCH,
    NOT_AWARDED_CANDIDATE,
    NOT_AWARD_EVIDENCE,
    UNKNOWN,
    UNPARSEABLE_AMOUNT,
    explain_rejection,
    near_miss_rejections,
    rejection_summary,
)

TARGET = {'brand': '湘仪', 'model': 'H1850R'}

# Verbatim from the frozen ON-arm source https://cg.qdu.edu.cn/jjjg/9920.chtml
QDU_BODY = (
    '采购预算 | ￥35,000.00 | 成交金额 | ￥32,980.00\n'
    '采购清单\n'
    '采购内容 | 品牌及型号 | 预算单价（元） | 采购数量 | 分项报价\n'
    '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00\n'
    '售后服务 | 整机质保一年\n'
)


def _source(body, **overrides):
    source = {'url': 'https://cg.qdu.edu.cn/jjjg/9920.chtml', 'body_text': body,
              'body_status': 'fetched', 'source_type': 'tender_document',
              'snapshot': {'sha256': 'a1610bffc9d1c71622430dfa92babc8efea71f82fbaead7b50eb0aed0dac5f6a'}}
    source.update(overrides)
    return source


# ---------------------------------------------------------------------------
# A losing bid is not a transaction.
#
# The 平顶山 announcement is titled 「中标（成交）结果公告」 and contains 中标/成交,
# so every row in it passed the ``kind='award'`` keyword gate -- including the
# 第二中标候选人's bid of 186,000 for the only H1850R in the document. The winner
# (第一中标候选人) never offered that model at all. That runner-up bid, pooled with
# a genuine 35,200 award, produced a bogus "35,200–186,000 range".
# ---------------------------------------------------------------------------

#: Header + row shape of the runner-up's list, as the production HTML cleaner
#: renders it (a Word table whose rank is a section label folded into the header).
PDS_BODY = (
    '中标（成交）结果公告\n'
    '包号 | 采购内容 | 供应商名称 | 中标金额\n'
    '1 | 大型生化免疫流水线后续设备 | 平顶山睿健医疗器械销售有限公司 | 5270000.00\n'
    '1 第一中标候选人 | 序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
    '6 | 台式高速冷冻大容量离心机 | 艾本德 | 5810R | 1 | 187500.00\n'
    '1 第二中标候选人 | 序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
    '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 186000.00\n'
    '1 第三中标候选人 | 序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
    '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 189100.00\n'
)


def _pds_row(amount):
    return {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': amount, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'date': None,
        'excerpt': f'6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | {amount:.2f}'.rstrip('0').rstrip('.'),
    }


def test_a_runner_up_bid_is_reported_as_not_awarded():
    """The 平顶山 row: a losing candidate's price must be refused, with the rank named."""
    body = PDS_BODY
    row = _pds_row(186000)
    # Use the real excerpt text so the row can be located in the body.
    row['excerpt'] = '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 186000.00'

    reason = explain_rejection(row, _source(body, url='http://ggzy.pds.gov.cn/zzbgs/47391.jhtml'), TARGET)

    assert reason['reason_code'] == NOT_AWARDED_CANDIDATE
    assert reason['candidate_rank'] == 2
    assert '第二中标候选人' in reason['detail']
    assert '未中标' in reason['detail']


def test_the_winners_own_list_is_still_locatable():
    """Sanity: the winner's row must NOT be attributed to a runner-up section.

    The section rule keys off the nearest preceding marker, so the winner's list
    must resolve to rank 1 -- otherwise the fix would silently drop the only
    usable prices in a candidate announcement.
    """
    from backend.services.procurement_tables import candidate_section_ordinal

    position = PDS_BODY.find('6 | 台式高速冷冻大容量离心机 | 艾本德 | 5810R')

    assert candidate_section_ordinal(PDS_BODY, position) == 1


def test_second_and_third_candidates_are_not_rank_one():
    from backend.services.procurement_tables import candidate_section_ordinal

    second = PDS_BODY.find('湘仪 | H1850R | 1 | 186000.00')
    third = PDS_BODY.find('湘仪 | H1850R | 1 | 189100.00')

    assert candidate_section_ordinal(PDS_BODY, second) == 2
    assert candidate_section_ordinal(PDS_BODY, third) == 3


def test_a_document_without_candidate_sections_is_unaffected():
    """No candidate markers means no attribution -- ordinary awards still work."""
    from backend.services.procurement_tables import candidate_section_ordinal

    body = ('中标（成交）结果公告\n'
            '1-7 | 医用低温、冷疗设备 | 高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00\n')

    assert candidate_section_ordinal(body, body.find('H1850R')) is None


def test_a_candidate_row_can_never_become_a_price():
    """The refusal must not carry a unit price -- it is display-only."""
    row = _pds_row(186000)
    row['excerpt'] = '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 186000.00'

    reason = explain_rejection(row, _source(PDS_BODY), TARGET)

    assert 'unit_price' not in reason


def test_a_row_listed_twice_under_different_candidates_fails_closed():
    """If the same row text sits in a loser's list too, refuse rather than accept.

    ``body.find`` alone would attribute an identical row to whichever section
    comes first (possibly the winner's). The conservative reading wins: accepting
    a losing bid corrupts the market number, while refusing loses one record.
    """
    from backend.services.price_rejections import candidate_rank_of

    identical = '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 186000.00'
    body = (
        '中标（成交）结果公告\n'
        '1 第一中标候选人 | 序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
        f'{identical}\n'
        '1 第二中标候选人 | 序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价（元）\n'
        f'{identical}\n'
    )

    assert body.count(identical) == 2
    assert candidate_rank_of({'excerpt': identical}, _source(body)) == 2


def test_model_mismatch_names_both_models():
    """The exact case the acceptance round asked to have explained."""
    row = {
        'brand': '中科中佳', 'model': 'HC-3016R', 'kind': 'award',
        'amount': 32980.0, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00',
    }

    reason = explain_rejection(row, _source(QDU_BODY), TARGET)

    assert reason['reason_code'] == MODEL_MISMATCH
    assert 'HC-3016R' in reason['other_models']
    # 比较的两边都要能看见：引文里的型号在 ``detail`` 上，本次核价用的目标型号在
    # 记录的 ``model`` 字段上（迭代规范 起不再在 ``detail`` 里重复写一遍称呼——报告 label
    # 与页面行首都读 ``model``）。
    assert 'HC-3016R' in reason['detail']
    assert reason['model'] == 'H1850R'
    assert 'H1850R' not in reason['detail']
    assert reason['url'] == 'https://cg.qdu.edu.cn/jjjg/9920.chtml'


def test_a_matching_model_is_never_reported_as_a_mismatch():
    body = ('成交结果公告\n'
            '1-7 | 医用低温、冷疗设备 | 高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00 | 70,400.00\n')
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 35200.0, 'amount_unit': '元', 'quantity': 2.0, 'amount_scope': 'unit',
        'excerpt': '1-7 | 医用低温、冷疗设备 | 高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00 | 70,400.00',
        'date': '2022-12-16',
    }

    reason = explain_rejection(row, _source(body, url='https://ggzyjy.sc.gov.cn/x'), TARGET)

    # A row that names the right model must never be blamed on a model mismatch.
    assert reason['reason_code'] != MODEL_MISMATCH
    assert not reason.get('other_models')


def test_brand_mismatch_is_reported_when_the_model_agrees():
    body = '成交结果公告\n台式高速冷冻离心机 | 中科中佳/H1850R | 35000 | 1 | ￥32,980.00\n'
    row = {
        'brand': '中科中佳', 'model': 'H1850R', 'kind': 'award',
        'amount': 32980.0, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '台式高速冷冻离心机 | 中科中佳/H1850R | 35000 | 1 | ￥32,980.00',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] == BRAND_MISMATCH
    assert '中科中佳' in reason['detail']


def test_budget_is_never_explained_as_an_achieved_price():
    body = '成交结果公告\n台式高速冷冻离心机 | 湘仪/H1850R | 预算单价（元）35000 | 1\n'
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 35000.0, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '台式高速冷冻离心机 | 湘仪/H1850R | 预算单价（元）35000 | 1',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] == BUDGET_NOT_ACHIEVED


def test_an_excerpt_absent_from_the_body_is_not_evidence():
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 9999.0, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '湘仪 H1850R 单价 9999 元（这段文字不在正文里）',
    }

    reason = explain_rejection(row, _source(QDU_BODY), TARGET)

    assert reason['reason_code'] == EXCERPT_NOT_IN_BODY


def test_an_unfetched_source_is_explained_not_silently_dropped():
    row = {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 1.0,
           'amount_unit': '元', 'amount_scope': 'unit', 'excerpt': '湘仪 H1850R'}

    reason = explain_rejection(row, _source('', body_status='unavailable'), TARGET)

    assert reason['reason_code'] == BODY_UNAVAILABLE


def test_a_missing_source_is_explained():
    row = {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 1.0,
           'amount_unit': '元', 'amount_scope': 'unit', 'excerpt': '湘仪 H1850R'}

    reason = explain_rejection(row, None, TARGET)

    assert reason['reason_code'] == BODY_UNAVAILABLE


def test_a_non_dict_row_does_not_raise():
    reason = explain_rejection('not-a-row', _source(QDU_BODY), TARGET)

    assert reason['reason_code'] == UNKNOWN


def test_an_unpriced_page_without_decision_wording_is_explained():
    body = '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | 面议\n'
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 1.0, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | 面议',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] == NOT_AWARD_EVIDENCE


def test_the_explanation_names_the_predicate_that_actually_refused_the_row():
    """解释必须指向 ``validate_price`` 真正停下的那一条判据。

    实测这条 row 停在币种绑定判据（引文是裸 ``35,200.00``，通篇没有「元」也没有
    ``￥``）。跟踪器定位到的拦截行是
    ``if raw.get("amount_unit") not in money_excerpt and not currency_prefix:``。
    解释器以前报的是 ``date_ungrounded``——一个 ``validate_price`` 根本不执行的判据：
    缺日期只把可比级别降到 ``model_reference`` 并记进 ``gaps``，不会拒收。
    """
    body = '中标结果公告 高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00\n'
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 35200.0, 'amount_unit': '元', 'quantity': 2.0, 'amount_scope': 'unit',
        'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00', 'date': '',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] == UNPARSEABLE_AMOUNT
    assert reason['reason_code'] != DATE_UNGROUNDED


def test_a_row_the_gate_accepted_is_never_given_a_refusal_reason():
    """通过验收口的 row 不得被编造一个拒绝理由。

    这是上一轮把根因写错的那条：CCGP 的 ``单价(元)：17480`` 行，``validate_price``
    返回了 quote（日期从公告正文抽到），解释器却说「没有可与公开证据对齐的成交日期」。
    这里公告正文里连日期都没有，验收口依然通过——所以日期从来不是拒绝点。
    """
    body = ('中标结果公告\n'
            '名称：血型血清学离心机 品牌：湘仪 规格型号：H1850R 数量：1 单价(元)：17480\n')
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 17480, 'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
        'excerpt': '品牌：湘仪 规格型号：H1850R 数量：1 单价(元)：17480', 'date': '',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] != DATE_UNGROUNDED
    assert '通过' in reason['detail']


def test_an_unknown_scope_is_reported_as_a_non_unit_amount():
    body = '中标结果公告 高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | ￥35,200.00\n'
    row = {
        'brand': '湘仪', 'model': 'H1850R', 'kind': 'award',
        'amount': 35200.0, 'amount_unit': '元', 'quantity': 1.0,
        'amount_scope': 'package_total', 'excerpt': '高速冷冻离心机 | 湘仪 | H1850R | 1 | 台 | ￥35,200.00',
        'date': '2022-12-16',
    }

    reason = explain_rejection(row, _source(body), TARGET)

    assert reason['reason_code'] == AMOUNT_NOT_UNIT_PRICE


def test_summary_counts_every_reason_and_orders_the_important_ones_first():
    items = [{'reason_code': MODEL_MISMATCH}, {'reason_code': MODEL_MISMATCH},
             {'reason_code': NOT_AWARD_EVIDENCE}]

    summary = rejection_summary(items)

    assert summary == [{'reason_code': MODEL_MISMATCH, 'count': 2},
                       {'reason_code': NOT_AWARD_EVIDENCE, 'count': 1}]


def test_every_explanation_carries_a_reason_and_a_detail():
    """A bare code the user cannot read would defeat the purpose."""
    rows = [
        {'brand': '中科中佳', 'model': 'HC-3016R', 'kind': 'award', 'amount': 32980.0,
         'amount_unit': '元', 'quantity': 1.0, 'amount_scope': 'unit',
         'excerpt': '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00'},
        {'brand': '湘仪', 'model': 'H1850R', 'kind': 'award', 'amount': 1.0,
         'amount_unit': '元', 'amount_scope': 'unit', 'excerpt': '湘仪 H1850R'},
    ]

    for row in rows:
        reason = explain_rejection(row, _source(QDU_BODY), TARGET)
        assert reason['reason_code']
        assert isinstance(reason['detail'], str) and reason['detail'].strip()


def test_near_miss_explains_a_tender_that_priced_a_sibling_model():
    """The upstream filter drops these before any predicate runs, so the
    explanation has to be derived separately or the user sees a bare 0 条."""
    source = _source(QDU_BODY, source_type='tender_document')

    found = near_miss_rejections([source], TARGET)

    assert found, 'the QCU announcement must produce an explanation'
    assert all(item['reason_code'] == MODEL_MISMATCH for item in found)
    assert any('HC-3016R' in item['detail'] for item in found)
    assert all('unit_price' not in item for item in found), '解释里不能出现单价'


def test_near_miss_ignores_sources_that_do_name_the_target_model():
    body = ('成交结果公告\n'
            '高速冷冻离心机 | 湘仪 | H1850R | 2.00(台) | 35,200.00\n')
    source = _source(body, source_type='tender_document')

    assert near_miss_rejections([source], TARGET) == []


def test_near_miss_ignores_non_tender_sources():
    source = _source(QDU_BODY, source_type='unknown')

    assert near_miss_rejections([source], TARGET) == []


def test_near_miss_finds_a_row_whose_header_is_not_a_unit_price():
    """分项报价 / 成交金额 columns carry a real amount with no 单价 heading."""
    body = ('成交结果公告\n'
            '采购内容 | 品牌及型号 | 预算单价（元） | 采购数量 | 分项报价\n'
            '台式高速冷冻离心机 | 中科中佳/HC-3016R | 35000 | 1 | ￥32,980.00\n')
    source = _source(body, source_type='tender_document')

    found = near_miss_rejections([source], TARGET)

    assert found
    assert 'HC-3016R' in found[0]['excerpt']
    assert '32,980' in found[0]['excerpt']


def test_parameter_values_are_never_reported_as_models():
    """技术参数列里的 55dB / 6x50ml / ISO9001 不是型号。

    真实公告的表格里这些值随手可得。把它们当成"型号不一致"讲给用户，等于
    编造一个不存在的型号；这条用例把这种误报钉死。
    """
    body = ('技术参数 | 数值 | 型号 | 单价（元）\n'
            '噪音 | 55dB | HC-3016R | 32980.00\n'
            '容量 | 6x50ml | HC-3016R | 32980.00\n'
            '认证 | ISO9001 | HC-3016R | 32980.00\n')
    source = _source(body, source_type='tender_document')

    found = near_miss_rejections([source], TARGET)
    reported = {model for item in found for model in item.get('other_models', [])}

    assert reported == {'HC-3016R'}, f'不得把参数当成型号：{reported}'
    # The excerpt is the verbatim original row, so it legitimately still contains
    # the parameter text. What must stay clean is what we *name* as a model.
    assert not any(junk in item['detail'] for item in found
                   for junk in ('55dB', '6x50ml', 'ISO9001'))


def test_a_priced_row_without_a_currency_marker_is_still_found():
    """表格单元格里是裸数字（25000.00），没有 ￥ 也没有"元"。

    真实公告里成交金额列就是这样写的；只认 ￥/元 会漏掉整张表。
    表头必须写明单位（``单价（元）``）——单独的"单价"没有币种上下文，
    项目现有规则不承认它是价格列，这里沿用该口径。
    """
    body = ('名称 | 型号 | 数量 | 单价（元）\n'
            '高速冷冻离心机 | GL0650R | 1 | 25000.00\n')
    source = _source(body, source_type='tender_document')

    found = near_miss_rejections([source], TARGET)

    assert found, '裸数字金额的表格也必须能被解释'
    assert any('GL0650R' in item['detail'] for item in found)


def test_a_model_near_an_amount_but_in_another_column_is_not_used_as_a_model():
    """型号与金额必须同属一行；参数列的值不得被拿来当型号。"""
    body = ('技术参数 | 数值 | 型号 | 单价（元）\n'
            '转速 | 12000rpm | HC-3016R | 32980.00\n')
    source = _source(body, source_type='tender_document')

    reported = {model for item in near_miss_rejections([source], TARGET)
                for model in item.get('other_models', [])}

    assert '12000rpm' not in reported


@pytest.mark.parametrize('token', [
    'hot122703', 'dtype1', 'gk1', '01000100000aa', 'QDU20260094',
    '6min', '50ml', 'min59', '55dB', '1sec', 'ISO9001', '10KV', '6x50ml',
    'AXF202600343',
])
def test_implausible_tokens_are_rejected_as_models(token):
    """这些 token 都是从真实公告里抓出来的噪音，不是型号。"""
    from backend.services.price_rejections import _is_plausible_model

    assert not _is_plausible_model(token, 'H1850R'), f'{token} 不该被当成型号'


@pytest.mark.parametrize('token', ['H1850R', 'HC-3016R', 'GL0650R', 'AC200', 'TGL-16G'])
def test_real_model_shapes_are_accepted(token):
    from backend.services.price_rejections import _is_plausible_model

    assert _is_plausible_model(token, 'OTHER-MODEL')


def test_the_target_model_is_never_reported_as_a_different_model():
    from backend.services.price_rejections import _is_plausible_model

    assert not _is_plausible_model('H1850R', 'H1850R')
