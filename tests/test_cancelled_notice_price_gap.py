# -*- coding: utf-8 -*-
"""做金额门时顺手找到的一条**既有**假阳性通道，现已由 ``validate_price`` 修掉。

它不是金额门那次放宽引入的价格通道：同一条"原中标候选人报价"，改动前就由
**表格分支**落地（``procurement_research.prices()`` 里跑在金额门之前的那一段，
金额门的 ``continue`` 只跳过模型分支）。那次放宽只是让**表格解析器读不出**的
废标公告也轮到模型抽一次，于是同一个既有缺口多了一条到达路径。

修复落在 ``validate_price``——所有价格通道的收口点，也是当初判定"真正该拦它的
是这里"的那个文件。它现在识别撤销语境：正文写着废标/流标/采购活动终止，且价格
与这段声明的相对位置说明价格属于被撤销的那一轮。判据基于**正文事实**而不是
域名，并且给「第一次废标、重新采购后成交」和「分包采购里某个包废标、另一个包
成交」留了豁免，见 ``tests/test_price_admissibility_revoked_notice.py``。

本文件原先用 ``xfail`` 标出这个缺口（写明"将来 ``validate_price`` 补上撤销语境
判定时这条用例会变成 XPASS，提醒把它改成正式断言"）。缺口补上后它确实 XPASS，
所以这里改成正式断言。
"""

from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

MODEL = 'Q10'
BRAND = '甲厂'

#: ``tests/test_end_to_end_adversarial.py`` 的 ``CANCELLED_HTML`` 语料去掉标签后的
#: 正文（同一份测试语料，不是真实公告样本）。「项目编号 + 采购人」是修复那次
#: 补上的结构字段：有了它们，这份废标公告放在 ``.gov.cn`` 主机上就是一个真正的
#: ``tender_document``，于是它会**真的**走到 ``validate_price``。
CANCELLED_BODY = '\n'.join([
    '某某市第一人民医院生物安全柜采购项目废标公告',
    '项目编号：HLJ-2026-0920　采购人：某某市第一人民医院',
    '因有效投标人不足三家，本项目废标，采购活动终止。',
    '原中标候选人报价如下（已废标，仅供参考）：',
    '货物名称 | 规格型号 | 数量 | 单价（元）',
    '生物安全柜 | 甲厂Q10 | 2 | 74,800',
    '公告日期：2026-09-20',
])


def test_a_revoked_quote_never_becomes_a_concluded_price(research):
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None

    class Empty:
        endpoint = 'test://cancelled-gap'
        model = 'counting'
        last_usage = None

        def analyze(self, **kwargs):
            return {'prices': []}          # 模型什么都不返回，价格只可能来自表格分支

    service.client_factory = Empty
    run['sources'] = [{'url': 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/cancelled.html',
                       'body_status': 'fetched', 'source_type': 'tender_document',
                       'body_text': CANCELLED_BODY}]

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})
    assert MODEL not in {p.get('model') for p in prices}


def test_that_test_corpus_is_readable_by_the_table_parser():
    """上面那条断言的前提：这条价格是表格分支自己就能读出来的。"""
    from backend.services.procurement_tables import price_table_rows
    assert len(price_table_rows(CANCELLED_BODY, MODEL)) == 1


def test_the_old_com_host_cancelled_notice_never_became_a_tender_document():
    """``tests/test_end_to_end_adversarial.py`` 里那条"废标价被拒"原先为什么通过。

    它注入的语料 host 是 ``www.zb-agent.com``，``is_tender`` 为假，``source_type``
    停在 ``unknown``，而 ``prices()`` 的来源白名单只收 ``tender_document`` 等三类——
    这份正文在任何价格判定之前就被排除了。通过的原因是**来源被排除**，不是价格被拒。

    更正一处归因：``is_tender`` 为假的直接原因**不是** ``.com`` 这个域名类别，而是
    正文措辞与结构字段。同一份正文放在 ``ggzyjyw.hlj.gov.cn`` 上同样是 False
    （下面第二条断言实测）；反过来说，补齐「项目编号 + 采购人」之后，``.gov.cn``
    上的同一份废标公告就成了真正的 ``tender_document``，此时唯一的守门人只能是
    ``validate_price``。
    """
    from backend.services.source_authority import is_tender
    bare = {'url': 'https://www.zb-agent.com/notice/2026-09-20',
            'body_status': 'fetched', 'body_text': CANCELLED_BODY.replace('项目编号：HLJ-2026-0920　', '')}
    assert is_tender(bare) is False
    on_gov = {**bare, 'url': 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/cancelled.html'}
    assert is_tender(on_gov) is False
    assert is_tender({'url': 'https://ggzyjyw.hlj.gov.cn/zbgg/2026/cancelled.html',
                      'body_status': 'fetched', 'body_text': CANCELLED_BODY}) is True
