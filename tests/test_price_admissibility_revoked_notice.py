# -*- coding: utf-8 -*-
"""废标 / 无效 / 已终止公告里的价格不得被当成成交价。

为什么单独一个文件
------------------
``tests/test_end_to_end_adversarial.py::test_a_cancelled_notices_price_is_refused``
原先的通过原因是**错的**：它注入的语料 host 是 ``www.zb-agent.com``，
``is_tender`` 对它为假（正文没有已决标措辞、结构性字段也不够），``source_type``
停在 ``unknown``，于是 ``prices()`` 的来源白名单在任何价格判定**之前**就把它排除
了。把 ``validate_price`` 的撤销判据整段删掉，它照样绿——它守的那道门从没被测到。

这里的断言直接落在 ``validate_price`` 上，另有一条端到端用例覆盖 ``prices()``
的表格分支。

语料说明
--------
本文件所有正文都是**合成正文**，不是真实公告样本；域名、字段名和措辞照真实
政府采购公告的写法构造（``.gov.cn`` 主机 + 项目编号 + 采购人，使
``source_authority.is_tender`` 真的返回 True，公告因此真的成为
``tender_document`` 并走进价格判定）。
"""
# 按模块属性调用而不是 ``from ... import revoked_price_evidence``：把生产代码的
# 撤销判据整段删掉时，这个文件要**收集成功、断言失败**（真实的行为回归），而不是
# 在 import 阶段就炸掉——后者会掩盖「哪一条断言守住了这个缺口」。
from backend.services import procurement_rules as rules
from backend.services.source_authority import is_tender
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

CANCELLED_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/cancelled-0911.html'
AWARD_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/award-0911.html'
REPURCHASE_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/award-0925.html'
LOTS_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/award-0924.html'
REVOKED_AFTER_AWARD_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/award-0914.html'
CONTRACT_END_URL = 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/contract-end.html'

ROW_LINE = '生物安全柜 | 甲厂Q10 | 2 | 74,800'
TARGET = {'brand': '甲厂', 'model': 'Q10'}
BASE_ROW = {
    'model': 'Q10', 'brand': '甲厂', 'amount': 74800, 'amount_unit': '元',
    'amount_scope': 'unit', 'quantity': 2, 'currency': 'CNY',
    'kind': 'award', 'excerpt': ROW_LINE,
}

#: 合成语料：一份已废标的公告，正文里留着「原中标候选人报价」表格。
#: 「项目编号」+「采购人」两个字段让 ``is_tender`` 对 ``.gov.cn`` 主机返回 True，
#: 所以这份正文会**真的**走到价格判定，而不是被来源白名单提前挡掉。
CANCELLED_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购项目废标公告',
    '项目编号：HLJ-2026-0911',
    '采购人：某某市第一人民医院',
    '因有效投标人不足三家，本项目废标，采购活动终止。',
    '原中标候选人报价如下（已废标，仅供参考）：',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '公告日期：2026-09-20',
])

#: 对照语料：同一个域名、同一张价格表、同一个金额，但正文是真实成交公告。
AWARD_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购项目中标（成交）结果公告',
    '项目编号：HLJ-2026-0911',
    '采购人：某某市第一人民医院',
    '中标（成交）供应商：某某医疗器械有限公司',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '公告日期：2026-09-11',
])

#: 反例①：第一次废标、重新采购后成交的正常公告。它的成交价**必须**被采纳。
REPURCHASE_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购项目（二次）中标（成交）结果公告',
    '项目编号：HLJ-2026-0911-B',
    '采购人：某某市第一人民医院',
    '本项目于2026-09-20废标，现重新组织采购。',
    '中标（成交）供应商：某某医疗器械有限公司',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '公告日期：2026-09-22',
])

#: 反例②：分包采购，第 1 包废标、第 2 包成交。第 2 包的成交价必须被采纳。
LOTS_BODY = '\n'.join([
    '某某市第一人民医院检验设备采购项目中标（成交）结果公告',
    '项目编号：HLJ-2026-0912',
    '采购人：某某市第一人民医院',
    '第1包（生物安全柜）因有效投标人不足三家废标。',
    '第2包中标供应商：某某医疗器械有限公司，中标金额：74,800元',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '公告日期：2026-09-21',
])

#: 反例③：先成交、后被监督检查处理决定废标。金额写在撤销声明**之前**，
#: 属于「已成交但被推翻」，必须拒。形态取自 ``tests/test_m04_official_search.py``
#: 里那条真实的柳州案例（那边走 official_search，本文件走 validate_price）。
REVOKED_AFTER_AWARD_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购项目成交公告',
    '项目编号：HLJ-2026-0914',
    '采购人：某某市第一人民医院',
    '中标（成交）供应商：某某医疗器械有限公司',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '| 根据某某市财政局文件（柳财采〔2026〕36号）的处理结果，本项目废标，重新组织采购活动。',
    '公告日期：2026-09-14',
])

#: 边界语料：合同终止公告。合同终止不是「本次采购未成交」，里面的金额是已签
#: 合同的成交价，判据**不**应把它当作撤销。
CONTRACT_END_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购合同终止公告',
    '项目编号：HLJ-2026-0916',
    '采购人：某某市第一人民医院',
    '合同金额：74,800元',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    ROW_LINE,
    '公告日期：2026-09-16',
])


def _source(url, body, source_type='tender_document'):
    return {'url': url, 'body_status': 'fetched', 'body_text': body,
            'source_type': source_type}


def _row(date):
    return {**BASE_ROW, 'date': date}


def test_the_synthetic_cancelled_notice_really_is_a_tender_document():
    """先把「这个语料真的走进了价格通道」钉死，否则后面的拒绝可能是别的原因。

    这条断言是修好原测试的**关键**：``is_tender`` 为 False 时 ``prices()`` 会在
    任何价格判定之前排除该来源，那种「拒绝」证明不了 ``validate_price`` 会拒。
    """
    source = _source(CANCELLED_URL, CANCELLED_BODY)
    assert is_tender(source) is True, '语料必须先成为 tender_document，判据才被测到'


def test_the_predicate_refuses_a_cancelled_notices_price():
    """撤销公告正文里的「原中标候选人报价」不是成交价。"""
    source = _source(CANCELLED_URL, CANCELLED_BODY)
    assert rules.validate_price(_row('2026-09-20'), source, TARGET) is None
    assert rules.revoked_price_evidence(ROW_LINE, [CANCELLED_BODY])


def test_the_same_price_is_adopted_when_the_notice_reports_a_conclusion():
    """同一个域名、同一张表、同一个金额，成交公告里必须照旧被采纳。

    没有这条，上面的「拒绝」可以靠「把来源整类挡掉」蒙过去。
    """
    source = _source(AWARD_URL, AWARD_BODY)
    quote = rules.validate_price(_row('2026-09-11'), source, TARGET)
    assert quote is not None
    assert quote['unit_price'] == 74800
    assert quote['kind'] == 'award' and quote['level'] == 'historical_configuration'


def test_a_repurchase_award_after_a_failed_tender_is_still_adopted():
    """反例①：第一次废标、重新采购后成交——正文里的成交价该采纳。

    撤销声明写在价格**之前**，但中间出现了「重新组织采购」和「中标（成交）
    供应商」，这两者都说明这个价格属于新一轮采购的结论。
    """
    source = _source(REPURCHASE_URL, REPURCHASE_BODY)
    quote = rules.validate_price(_row('2026-09-22'), source, TARGET)
    assert quote is not None and quote['unit_price'] == 74800


def test_a_second_lots_award_survives_a_cancelled_first_lot():
    """反例②：分包采购里第 1 包废标、第 2 包成交——第 2 包的成交价该采纳。"""
    source = _source(LOTS_URL, LOTS_BODY)
    quote = rules.validate_price(_row('2026-09-21'), source, TARGET)
    assert quote is not None and quote['unit_price'] == 74800


def test_an_amount_revoked_after_it_was_awarded_is_refused():
    """反例③：成交后被监督检查处理决定废标——金额在撤销声明之前也要拒。"""
    source = _source(REVOKED_AFTER_AWARD_URL, REVOKED_AFTER_AWARD_BODY)
    assert rules.validate_price(_row('2026-09-14'), source, TARGET) is None


def test_a_contract_termination_is_not_read_as_a_procurement_revocation():
    """边界：合同终止 ≠ 本次采购未成交，判据不拿裸「终止」两个字作废整篇。"""
    source = _source(CONTRACT_END_URL, CONTRACT_END_BODY)
    assert rules.revoked_price_evidence(ROW_LINE, [CONTRACT_END_BODY]) is None


def test_an_attachment_price_under_a_cancelled_notice_is_refused():
    """挂在废标公告下的「报价明细」附件同样不是成交价。

    附件自己那几行可能一个「废标」都不含，撤销语境在父公告里；
    ``fetch_attachments`` 会把父公告正文放进 ``publication_context``，所以判据
    必须看得到它，也必须**分开**判定两份文本（见 ``revoked_price_evidence``）。
    """
    attachment_url = CANCELLED_URL + '/files/detail.html'
    attachment_body = '\n'.join([
        '某某市第一人民医院生物安全柜采购项目报价明细',
        '货物名称 | 规格型号 | 数量 | 单价（元）',
        ROW_LINE,
        '公告日期：2026-09-11',
    ])
    source = {
        'url': attachment_url, 'body_status': 'fetched', 'source_type': 'tender_document',
        'body_text': attachment_body, 'parent_url': CANCELLED_URL,
        'publication_context': {'url': CANCELLED_URL, 'body_text': CANCELLED_BODY,
                                'linked_url': attachment_url},
    }
    assert rules.validate_price(_row('2026-09-11'), source, TARGET) is None
    # 同一条附件挂在成交公告下就照旧被采纳——撤销判据跟的是父公告的语境，
    # 不是附件的 URL 形态。
    source['publication_context'] = {'url': AWARD_URL, 'body_text': AWARD_BODY,
                                     'linked_url': attachment_url}
    source['parent_url'] = AWARD_URL
    assert rules.validate_price(_row('2026-09-11'), source, TARGET) is not None


def test_the_price_lane_returns_nothing_for_a_revoked_notice(research):  # noqa: F811
    """端到端：``prices()`` 的表格分支不得把废标公告的价格交出来。

    模型被换成返回空 ``{"prices": []}`` 的桩，所以这条价格只可能来自表格分支
    （``procurement_research.prices()`` 里在金额门之前运行的那一段）。
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None

    class Empty:
        endpoint = 'test://revoked-notice'
        model = 'counting'
        last_usage = None

        def analyze(self, **kwargs):
            return {'prices': []}

    service.client_factory = Empty
    source = _source(CANCELLED_URL, CANCELLED_BODY)
    assert is_tender(source) is True
    run['sources'] = [source]

    assert service.prices(run, TARGET) == []
