"""价格抽取之前，替漏抓的公告补一次附件。

``fetch_attachments`` 只在**正文到手的那一刻**被调用。公告进入一轮研究的时机
不止一个，而下面这几种都在各自调用点之后：

* 聚合站线索复核回来的官方原文（``aggregator_leads`` 直接 ``fetch_source``，
  从不调 ``fetch_attachments``）；
* 失败正文的重试（``recover_sources``）；
* 附件读取被 fetch 额度挡掉的公告——正文到手了，附件却停在「有名字、没内容」
  （实测 ``zbcg.wit.edu.cn/jgggwtcg/1781.chtml``：来源清单里有它、附件记录零条，
  而那一轮的 fetch 额度已经跑满 72/72）。

它们从此没人再替它抓附件，正文里的链接一直摆着，附件那一行始终没有正文。
``backfill_publication_attachments`` 在价格抽取之前把这件事补上，判据一个字不改。
"""
import pytest

from backend.services.procurement_rules import validate_price
from backend.services.procurement_tables import price_table_rows
from backend.services.source_authority import linked_publication
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

NOTICE_URL = 'https://www.example-zb.com/notice/award-0417'
ATTACHMENT_URL = 'https://www.example-zb.com/notice/files/detail-0417.xlsx'
# 相对 href：发布方的普通写法，绝对地址不在正文里。
NOTICE_BODY = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '成交供应商：某某医疗器械有限公司\n'
    '中标金额：人民币 236,000.00 元\n'
    # 正文里要有目标型号：附件闸门先确认"这份公告跟目标型号有关"才肯花额度读附件
    # （设计规范：「先确保有相关型号的公告再抓取配置附件」）。真实的
    # 中标公告本来就会写主要标的的型号，所以这份 fixture 也该写。
    '主要标的：生物安全柜 甲厂Q10 2 台\n'
    '附件信息：\n'
    '[报价明细](files/detail-0417.xlsx)\n'
)
DETAIL_TABLE = (
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 甲厂Q10 | 2 | 74,800\n'
)
BASELINE = {'brand': '甲厂', 'model': 'Q10'}


def award_row(body):
    return {**price_table_rows(body, 'Q10')[0], 'kind': 'award'}


def put(run, source):
    run['sources'] = [s for s in run['sources'] if s['url'] != source['url']] + [source]


def notice(source_type='tender_document'):
    return {'url': NOTICE_URL, 'source_type': source_type, 'body_status': 'fetched',
            'body_text': NOTICE_BODY, 'purpose': 'tender'}


def test_a_notice_whose_attachment_never_arrived_is_backfilled_once(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: DETAIL_TABLE}
    put(run, notice())

    assert service.backfill_publication_attachments(run) == [NOTICE_URL]

    attachment = next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)
    assert attachment['body_status'] == 'fetched'
    assert attachment['parent_url'] == NOTICE_URL
    # 继承父页面的身份，和走正常路径抓到的附件一模一样。
    assert attachment['source_type'] == 'tender_document'
    assert linked_publication(attachment) is not None
    # 落库了：补抓不是只活在内存里。
    stored = service.get(run['task_id'], run['id'])
    assert any(s['url'] == ATTACHMENT_URL for s in stored['sources'])


def test_a_backfilled_attachment_still_faces_the_unchanged_price_rules(research):  # noqa: F811
    """补抓只负责把正文拿回来；能不能成为价格仍由既有判据回答。"""
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: DETAIL_TABLE}
    put(run, notice())
    service.backfill_publication_attachments(run)
    attachment = next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)

    assert validate_price(award_row(DETAIL_TABLE), attachment, BASELINE)['unit_price'] == 74800
    # 反例：没有父页面身份，同一张表被判据拒绝。补抓没有放宽任何条件。
    assert validate_price(award_row(DETAIL_TABLE), {**attachment, 'parent_url': None}, BASELINE) is None
    assert validate_price(award_row(DETAIL_TABLE), {**attachment, 'source_type': 'unknown'}, BASELINE) is None


def test_a_permanently_failing_attachment_is_attempted_only_once(research):  # noqa: F811
    """``fetch_attachments`` 自带的重试跳过只看 fetched，所以幂等要靠这笔记录。"""
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY}       # 附件永远读不到
    put(run, notice())

    assert service.backfill_publication_attachments(run) == [NOTICE_URL]
    attachment = next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)
    assert attachment['body_status'] != 'fetched'     # failed / unavailable
    readings = len(provider.reads)

    assert service.backfill_publication_attachments(run) == []
    assert len(provider.reads) == readings
    stored = service.get(run['task_id'], run['id'])
    assert stored['attachment_backfill_attempted'] == [NOTICE_URL]


def test_a_notice_that_already_has_an_attachment_body_is_not_backfilled(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: DETAIL_TABLE}
    put(run, notice())
    service.backfill_publication_attachments(run)
    readings = len(provider.reads)

    assert service.backfill_publication_attachments(run) == []
    assert len(provider.reads) == readings


@pytest.mark.parametrize('overrides', [
    {'body_text': '某某仪器商城 现货 加入购物车\n[报价明细](files/d.xlsx)\n'},   # 没有公告用语
    {'body_text': NOTICE_BODY.replace('附件信息：\n[报价明细](files/detail-0417.xlsx)\n', '')},
    {'body_status': 'failed'},
])
def test_pages_that_do_not_qualify_are_left_alone(research, overrides):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: DETAIL_TABLE}
    put(run, {**notice(), **overrides})

    assert service.backfill_publication_attachments(run) == []
    assert not any(s['url'] == ATTACHMENT_URL for s in run['sources'])
    assert len(provider.reads) == 0


def test_a_manufacturer_page_is_not_a_publication_and_is_left_alone(research):  # noqa: F811
    """公告判据与闸门同源：厂商页没有成交措辞就不是公告，补抓也不碰它。"""
    service, run, provider, _ = research
    page_url = 'https://www.thermofisher.com/product/q10'
    spec_url = 'https://www.thermofisher.com/product/files/spec.pdf'
    provider.bodies = {page_url: 'Q10 技术规格\n[规格书](files/spec.pdf)',
                       spec_url: 'Q10 容量 120 ml'}
    put(run, {'url': page_url, 'source_type': 'manufacturer_website', 'body_status': 'fetched',
              'body_text': provider.bodies[page_url]})

    assert service.backfill_publication_attachments(run) == []
    assert not any(s['url'] == spec_url for s in run['sources'])
