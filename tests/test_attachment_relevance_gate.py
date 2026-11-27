"""抓附件之前先确认这份公告跟目标型号有关。

设计规范：「先确保有相关型号的公告再抓取配置附件」。

原来的闸门是"来源类型是招标文件就放行"，于是按关键词捞回来的**别的项目**的招标文件
也照抓——批3 实测抓到一份 77356 字的"智慧互联白板一体机"招标文件和一份 26413 字的
马铃薯基地项目文件，通篇目标型号出现 0 次，读取额度白花。

分两档，测试把两档都钉住：
* 公告正文含目标型号（或品牌+品类、或附件文件名含型号）→ 按原额度读；
* 只是"像官方采购公告"、正文没写型号 → **只读一份**。「型号只写在附件里、公告正文
  不写」是真实情形（``test_attachment_chain.py`` 讲的就是这一类），一刀切不读会漏配置。
"""
from tests.test_attachment_backfill import (  # noqa: F401 - 复用同一份 fixture
    ATTACHMENT_URL, DETAIL_TABLE, NOTICE_BODY, NOTICE_URL, notice, put, research,
)

SECOND_ATTACHMENT_URL = 'https://www.example-zb.com/notice/files/config-0417.xlsx'
# 一份不提型号的官方公告：正文里只有项目名、金额和两个附件链接。
NOTICE_WITHOUT_MODEL = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '中标金额：人民币 236,000.00 元\n'
    '附件信息：\n'
    '[报价明细](files/detail-0417.xlsx)\n'
    '[配置清单](files/config-0417.xlsx)\n'
)
# 既不含型号、发布方也不是官方采购平台：这是"按关键词捞回来的别的项目文件"。
UNRELATED_URL = 'https://random-blog-12345.example.org/post/notice.html'
UNRELATED_BODY = NOTICE_WITHOUT_MODEL.replace('某某市第一人民医院', '某某大学食堂改造')


def attachment_reads(provider):
    return [url for url in provider.reads if '/files/' in url]


def test_a_notice_naming_the_model_reads_its_attachments(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: DETAIL_TABLE}
    put(run, notice())

    service.fetch_attachments(run, notice())

    assert attachment_reads(provider) == [ATTACHMENT_URL], '正文写了型号，附件照读'


def test_an_official_notice_without_the_model_reads_only_one_attachment(research):  # noqa: F811
    """「型号只在附件里」是真实情形，所以还读——但只读一份，不整批花额度。"""
    service, run, provider, _ = research
    source = {'url': NOTICE_URL, 'source_type': 'tender_document', 'body_status': 'fetched',
              'body_text': NOTICE_WITHOUT_MODEL, 'purpose': 'tender'}
    provider.bodies = {NOTICE_URL: NOTICE_WITHOUT_MODEL, ATTACHMENT_URL: DETAIL_TABLE,
                       SECOND_ATTACHMENT_URL: DETAIL_TABLE}
    put(run, source)

    service.fetch_attachments(run, source)

    assert len(attachment_reads(provider)) == 1, '只读最像明细的那一份'


def test_an_unrelated_page_costs_no_attachment_reads(research):  # noqa: F811
    service, run, provider, _ = research
    source = {'url': UNRELATED_URL, 'source_type': 'unknown', 'body_status': 'fetched',
              'body_text': UNRELATED_BODY, 'purpose': 'tender'}
    provider.bodies = {UNRELATED_URL: UNRELATED_BODY,
                       'https://random-blog-12345.example.org/post/files/detail-0417.xlsx': DETAIL_TABLE}
    put(run, source)

    service.fetch_attachments(run, source)

    assert attachment_reads(provider) == [], '公告跟目标型号无关就不花额度'
