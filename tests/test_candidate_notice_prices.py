"""A whole notice that is only a 中标候选人公示 must not price a losing bid.

The two writings that had no real sample behind them both reduce to one defect:
``validate_price`` had no structural test for *whose* bid a row is, only the
wording test ``中标|成交`` -- which the word 中标 inside 「中标候选人公示」 satisfies
on its own.

Real sample (fetched with the product's own ``PublicPageReader`` and cleaned by
the product's own ``_clean_html``; the notice body is reproduced verbatim below):

    芜湖市第一人民医院 心电监护、输液泵双泵采购（二包）中标候选人公示
    https://whfph.com/714/15595.html                      发布 2024-05-17

    第一中标候选人 | 投标报价 | 104750.00元
    第一中标候选人 | 主要标的信息 | 名称：输液泵品牌：科曼 规格型号：ME660A 数量：25台 单价：1520元
    第二中标候选人 | 投标报价 | 99000.00元
    第二中标候选人 | 主要标的信息 | 名称：输液泵品牌：路迈 规格型号：LS-1 数量：25台 单价：1780元

The notice carries no 成交 and no award amount of its own: the only 中标结果
wording on the page is the footer link to the result announcement
(https://whfph.com/714/15599.html), whose 中标金额 is 104,750.00 元 -- exactly the
第一中标候选人's 投标报价 -- and that page publishes no model and no unit price.
So the loser's 路迈 LS-1 at 1,780 元/台 was never bought. Before this fix the
model-extraction lane adopted it (``validate_price`` returned 1,780 with
``level='historical_configuration'``), while the table lane could not even see it:
the amount sits in a free-text cell, so ``price_table_rows`` returns 0 rows for
both models, and the table lane's ``candidate_rank_of`` pre-gate therefore never
ran on this document.

The refusal lives in ``procurement_rules.validate_price`` because that is the one
predicate stack every price lane runs through -- the table lane and the
model-extraction lane both -- so a third lane added later cannot miss it. The
table lane's own pre-gate in ``procurement_research.prices`` is left unchanged; it
now agrees with the choke point instead of being the only copy.
"""
import pytest

from backend.services.price_rejections import (
    NOT_AWARDED_CANDIDATE,
    candidate_rank_of,
    explain_rejection,
)
from backend.services.procurement_rules import validate_price
from backend.services.procurement_tables import (
    candidate_section_ordinal,
    price_table_rows,
)
from tests.test_price_rejections import PDS_BODY
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

URL = 'https://whfph.com/714/15595.html'

#: Verbatim notice body: lines 106-135 of the cleaned snapshot of the live page
#: above. Page navigation, footer and the contact-person/phone lines (which no
#: predicate reads) are omitted; no notice wording was edited.
WHFPH_BODY = (
    '来源：\n'
    '发表时间：2024-05-17 09:08\n'
    '浏览次数：6592\n'
    '项目名称 | 芜湖市第一人民医院心电监护、输液泵双泵采购（二包） | 芜湖市第一人民医院心电监护、输液泵双泵采购（二包）\n'
    '项目编号 | WHYZ(2024)02602 | WHYZ(2024)02602\n'
    '标段名称 | 芜湖市第一人民医院心电监护、输液泵双泵采购（二包） | 芜湖市第一人民医院心电监护、输液泵双泵采购（二包）\n'
    '标段编号 | WHYZ(2024)02602 | WHYZ(2024)02602\n'
    '招标人 | 名称 | 芜湖市第一人民医院\n'
    '招标人 | 地址 | 安徽省芜湖市赤铸山东路1号\n'
    '招标代理机构 | 名称 | 芜湖宜正工程咨询有限公司\n'
    '招标代理机构 | 地址 | 芜湖市鸠江区皖江财富广场A3座3层302A室\n'
    '招标方式 | 公开招标 | 公开招标\n'
    '开标时间 | 2024年05月16日 | 2024年05月16日\n'
    '第一中标候选人 | 单位名称 | 合肥市轩辕医疗器械有限公司\n'
    '第一中标候选人 | 投标报价 | 104750.00元\n'
    '第一中标候选人 | 供货期 | 按院方实际需求分批次供货，接到供货通知一个星期内完成安装调试。\n'
    '第一中标候选人 | 主要标的信息 | 名称：输液泵品牌：科曼 规格型号：ME660A 数量：25台 单价：1520元\n'
    '第一中标候选人 | 规定公示的单位业绩 | 1、宁国市人民医院采购合同，2019.5；2、宁国市人民医院设备采购合同，2020.11。\n'
    '第二中标候选人 | 单位名称 | 合肥迪迈康医疗科技有限公司\n'
    '第二中标候选人 | 投标报价 | 99000.00元\n'
    '第二中标候选人 | 供货期 | 按院方实际需求分批次供货，接到供货通知一个星期内完成安装调试。\n'
    '第二中标候选人 | 主要标的信息 | 名称：输液泵品牌：路迈 规格型号：LS-1 数量：25台 单价：1780元\n'
    '第二中标候选人 | 规定公示的单位业绩 | 1、西安交通大学第一附属医院设备购置合同，2022.4；2、马鞍山市妇幼保健院设备采购合同，2024.3。\n'
    '评标被否决单位及原因 | 无 | 无\n'
    '公示时间 | 公示发布次日起3日 | 公示发布次日起3日\n'
    '其他招标文件规定的公示项 | 无 | 无\n'
)

WINNER_EXCERPT = ('第一中标候选人 | 主要标的信息 | 名称：输液泵品牌：科曼 '
                  '规格型号：ME660A 数量：25台 单价：1520元')
LOSER_EXCERPT = ('第二中标候选人 | 主要标的信息 | 名称：输液泵品牌：路迈 '
                 '规格型号：LS-1 数量：25台 单价：1780元')

#: The 平顶山 announcement's table geometry (real body pinned by
#: ``tests/test_price_rejections.py``), written as a column-aligned table so the
#: projector can reach the row: the fixture there is abridged to 7 header cells /
#: 6 data cells, which ``price_table_rows`` refuses on the ragged-row rule, so only
#: the section label differs between the two bodies below.
CANDIDATE_TABLE = (
    '中标（成交）结果公告\n'
    '1 {label} | 货物名称 | 规格型号 | 品牌 | 数量 | 单价（元）\n'
    '6 | 高速冷冻离心机 | H1850R | 湘仪 | 1 | 186000.00\n'
)
TABLE_ROW = '6 | 高速冷冻离心机 | H1850R | 湘仪 | 1 | 186000.00'


def _source(body=WHFPH_BODY, **overrides):
    source = {'url': URL, 'body_text': body, 'body_status': 'fetched',
              'source_type': 'tender_document', 'publication_date': '2024-05-17'}
    source.update(overrides)
    return source


def _row(excerpt, brand, model, amount):
    return {'brand': brand, 'model': model, 'kind': 'award', 'amount': amount,
            'amount_unit': '元', 'quantity': 25.0, 'amount_scope': 'unit',
            'currency': 'CNY', 'date': '2024-05-17', 'excerpt': excerpt}


LOSER_ROW = _row(LOSER_EXCERPT, '路迈', 'LS-1', 1780.0)
WINNER_ROW = _row(WINNER_EXCERPT, '科曼', 'ME660A', 1520.0)


def test_the_losing_candidates_unit_price_is_refused():
    """The 第二中标候选人's 1,780 元/台 was never bought: the 第一名 took the 包."""
    assert candidate_rank_of(LOSER_ROW, _source()) == 2

    assert validate_price(LOSER_ROW, _source(), {'brand': '路迈', 'model': 'LS-1'}) is None

    reason = explain_rejection(LOSER_ROW, _source(), {'brand': '路迈', 'model': 'LS-1'})
    assert reason['reason_code'] == NOT_AWARDED_CANDIDATE
    assert reason['candidate_rank'] == 2
    assert '第二中标候选人' in reason['detail']
    assert 'unit_price' not in reason, '拒绝理由绝不能带出单价'


def test_the_first_candidates_unit_price_is_still_accepted():
    """Rank 1 stays admissible: the 第一名's bid *is* the award amount here.

    The same project's result announcement (https://whfph.com/714/15599.html)
    publishes 中标金额 104,750.00 元, byte-identical to this notice's
    第一中标候选人 投标报价 104,750.00 元, so refusing the first candidate would
    throw away the real deal in the ordinary case.
    """
    assert candidate_section_ordinal(WHFPH_BODY, WHFPH_BODY.find(WINNER_EXCERPT)) == 1
    assert candidate_rank_of(WINNER_ROW, _source()) is None

    checked = validate_price(WINNER_ROW, _source(), {'brand': '科曼', 'model': 'ME660A'})

    assert checked is not None
    assert checked['unit_price'] == 1520.0
    assert checked['amount_scope'] == 'unit'


def test_a_notice_without_candidate_ranking_is_untouched():
    """``None`` means "no ranking stated" and must keep admitting normal awards.

    Real shape already accepted by ``tests/test_four_module_boundaries.py``.
    """
    body = ('成交公告 2023-09-14 金额单位：元\n项目总额37799元\n'
            '|品牌|型号|数量|单价|\n|天津泰斯特|SPX-150BIII|1|6800|')
    source = _source(body)
    row = {**price_table_rows(body, 'SPX-150BIII')[0], 'kind': 'award', 'date': '2023-09-14'}

    assert candidate_rank_of(row, source) is None
    checked = validate_price(row, source, {'brand': '泰斯特', 'model': 'SPX-150BIII',
                                           'supplier_price': 6800, 'currency': 'CNY'})

    assert checked is not None and checked['unit_price'] == 6800


def test_one_table_row_is_admitted_under_the_winner_and_refused_under_a_runner_up():
    """The same row and the same body shape; only the candidate section differs.

    This is what proves the refusal comes from the ranking rather than from some
    other predicate: with a 第一中标候选人 label the row is admitted, with a
    第二中标候选人 label the identical row is refused.
    """
    row = _row(TABLE_ROW, '湘仪', 'H1850R', 186000.0)
    row['quantity'] = 1.0
    row['date'] = None
    winner_body = CANDIDATE_TABLE.format(label='第一中标候选人')
    loser_body = CANDIDATE_TABLE.format(label='第二中标候选人')

    assert candidate_section_ordinal(winner_body, winner_body.find(TABLE_ROW)) == 1
    accepted = validate_price(row, _source(winner_body), {'brand': '湘仪', 'model': 'H1850R'})
    assert accepted is not None and accepted['unit_price'] == 186000.0

    assert candidate_section_ordinal(loser_body, loser_body.find(TABLE_ROW)) == 2
    assert validate_price(row, _source(loser_body), {'brand': '湘仪', 'model': 'H1850R'}) is None


def test_the_real_pds_runner_up_row_is_attributed_to_rank_two():
    """The pinned 平顶山 fixture: attribution must keep naming the runner-up."""
    excerpt = '6 | 台式高速冷冻大容量离心机 | 湘仪 | H1850R | 1 | 186000.00'
    row = _row(excerpt, '湘仪', 'H1850R', 186000.0)
    row['quantity'] = 1.0

    assert candidate_section_ordinal(PDS_BODY, PDS_BODY.find(excerpt)) == 2
    assert candidate_rank_of(row, _source(PDS_BODY)) == 2


def test_the_model_lane_refuses_the_loser_and_records_the_reason(research):  # noqa: F811
    """``prices()`` must not only drop the row -- ``price_rejections`` must say why.

    This is the lane that used to adopt it: ``procurement_research.prices`` hands
    the model-extraction rows straight to ``validate_price`` (line 942), unlike the
    table rows, which pass the pre-gate at line 912.
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    run['sources'] = [_source()]
    service.model = lambda run, prompt, data: {'prices': [dict(LOSER_ROW)]}

    prices = service.prices(run, {'brand': '路迈', 'model': 'LS-1'})

    assert prices == []
    recorded = [item for item in run['price_rejections']
                if item['reason_code'] == NOT_AWARDED_CANDIDATE]
    assert recorded, f'落选者的拒绝理由必须可见：{run["price_rejections"]}'
    assert recorded[0]['excerpt'].startswith('第二中标候选人')
    assert 'unit_price' not in recorded[0], '拒绝理由绝不能带出单价'


def test_the_model_lane_still_adopts_the_first_candidates_price(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    run['sources'] = [_source()]
    service.model = lambda run, prompt, data: {'prices': [dict(WINNER_ROW)]}

    prices = service.prices(run, {'brand': '科曼', 'model': 'ME660A'})

    assert [price['unit_price'] for price in prices] == [1520.0]


@pytest.mark.parametrize('probe,expected', [(LOSER_EXCERPT, 2), (WINNER_EXCERPT, 1)])
def test_the_ranking_is_read_from_the_real_notice_body(probe, expected):
    assert candidate_section_ordinal(WHFPH_BODY, WHFPH_BODY.find(probe)) == expected


#: Real notice, fetched the same way (verbatim key lines):
#: 黑龙江公共资源交易网 2026-03-31 「中标人放弃中标资格顺延第二中标候选人为中标人的公示」
#: https://ggzyjyw.hlj.gov.cn/jyfwdt/003002/003002001/003002001003/20260331/
#:   9a401cea-51da-46b1-b5d7-a2a0689c54a5.html
SHIFTED_BODY = (
    '关于 逊克县宝山至阿廷河林场（ Y004）公路提质改造工程项目\n'
    '中标人放弃中标资格顺延第二中标候选人为中标人的公示\n'
    '项目名称 ： 逊克县宝山至阿廷河林场（ Y004）公路提质改造工程\n'
    '项目编号 ： QT1106G251028001002\n'
    '本项目于 2026 年 02 月 11 日完成开评标工作，并依法公示中标候选人。公示期间无异议，'
    '于 2026 年 02 月 26 日向第一中标候选人 黑龙江仁硕建筑工程有限公司 发出中标通知书。'
    '现因该中标人自愿放弃中标资格，根据《中华人民共和国招标投标法实施条例》相关规定及'
    '招标文件约定，招标人决定顺延第二中标候选人 江西中煤建设集团有限公司 为本项目中标人。\n'
    '一、中标人信息\n'
    '1. 中标单位： 江西中煤建设集团有限公司\n'
    '2. 中标金额： 4729098.13元\n'
)


def test_a_shifted_award_is_refused_like_a_losing_bid_known_limitation():
    """本门的已知代价：把中标资格顺延给第二候选人时，真实成交价也会被拒。

    这份真实公告确实成交，而且中标金额就写在「第二中标候选人」这个标记之后，
    所以 ``candidate_rank_of`` 会把它当成落选投标，``validate_price`` 一并拒掉。
    这是"拿不到排名就放行、拿到非第一排名就拒绝"这条失败关闭规则的代价：宁可
    少一条真实成交价，也不能把落选投标价当成交价。要区分「顺延中标」与「落选
    投标」，需要第二条规则（文档是否把该候选人写成中标人/成交供应商），本轮不
    实现——这里只把事实固定下来，免得这个代价被悄悄忘掉。
    """
    excerpt = '2. 中标金额： 4729098.13元'
    row = {'brand': '', 'model': 'QT1106G251028001002', 'kind': 'award', 'amount': 4729098.13,
           'amount_unit': '元', 'quantity': None, 'amount_scope': 'unit',
           'currency': 'CNY', 'date': '2026-03-31', 'excerpt': excerpt}

    assert excerpt in SHIFTED_BODY
    assert '顺延第二中标候选人' in SHIFTED_BODY
    assert candidate_rank_of(row, _source(SHIFTED_BODY)) == 2
    assert validate_price(row, _source(SHIFTED_BODY),
                          {'brand': '', 'model': 'QT1106G251028001002'}) is None
