"""Step two of the chain: 拿到 -- the price table lives in the attachment.

The unit price of a single instrument is almost never in the notice body; it
sits in the table the notice attaches. Three things have to hold, and each one
was broken:

1. the attachment is *discovered* -- the price table is routinely not among the
   publisher's first three files, and the publisher's file handler often has no
   suffix to prove what it is;
2. the attachment *inherits the parent's identity* -- a price table does not
   call itself an award notice, so its own bytes say ``unknown``;
3. the parent's award wording is lent through the *recorded* link. The previous
   rule searched the parent body for the absolute attachment URL, which fails
   on every site that writes a relative href -- that is, on the ordinary case.
   These fixtures use relative links on purpose, so this file fails against the
   old rule for the real reason rather than against a convenient one.
"""
import pytest

from backend.services.procurement_rules import validate_price
from backend.services.procurement_tables import price_table_rows
from backend.services.research_sources import attachments, is_document_link
from backend.services.source_authority import linked_publication
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

# The notice the publisher posted. It announces the award and names no model:
# the line items are in the attached table, which is the normal arrangement.
AGENCY_NOTICE = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '成交供应商：某某医疗器械有限公司\n'
    '中标金额：人民币 236,000.00 元\n'
    '公告日期：2026年09月18日\n'
    '附件信息：\n'
    '[报价明细](files/detail-0417.xlsx)\n'      # relative href: the ordinary case
)

# The attached table: a price with no announcement vocabulary of its own.
DETAIL_TABLE = (
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 甲厂Q10 | 2 | 74,800\n'
)
BASELINE = {'brand': '甲厂', 'model': 'Q10'}


def award_row(body):
    return {**price_table_rows(body, 'Q10')[0], 'kind': 'award'}


# --------------------------------------------------------------------------
# Forward: the publisher's own attachment becomes usable price evidence.
# --------------------------------------------------------------------------

def test_relative_attachment_keeps_the_parent_award_wording(research):  # noqa: F811
    service, run, provider, _ = research
    parent_url = 'https://www.example-zb.com/notice/20260918-award'
    attachment_url = 'https://www.example-zb.com/notice/files/detail-0417.xlsx'
    provider.bodies = {parent_url: AGENCY_NOTICE, attachment_url: DETAIL_TABLE}

    parent = service.fetch_source(run, {'url': parent_url, 'source_type': 'unknown'})
    run['sources'].append(parent)
    assert parent['source_type'] == 'tender_document'
    # Why this fixture is the ordinary case: the absolute URL the crawler
    # needs never appears in the body a relative link produces.
    assert attachment_url not in parent['body_text']

    service.fetch_attachments(run, parent)
    attachment = next(s for s in run['sources'] if s['url'] == attachment_url)

    assert attachment['body_status'] == 'fetched'
    assert attachment['parent_url'] == parent_url
    # Inherited, though its own bytes are a price table and nothing else.
    assert attachment['source_type'] == 'tender_document'
    assert linked_publication(attachment) is not None

    accepted = validate_price(award_row(DETAIL_TABLE), attachment, BASELINE)
    assert accepted['unit_price'] == 74800

    # The causal control: the table alone carries no award wording, so without
    # the recorded parent the same table is priced out.
    assert validate_price(award_row(DETAIL_TABLE), {**attachment, 'parent_url': None}, BASELINE) is None
    assert validate_price(award_row(DETAIL_TABLE), {**attachment, 'source_type': 'unknown'}, BASELINE) is None


def test_attachment_budget_follows_the_publishers_file_list():
    links = [
        '[采购文件](files/a.pdf)',
        '[招标文件](files/b.docx)',
        '[其他公告](files/c.pdf)',
        '[报价明细](files/d.xlsx)',
        '[中标结果](https://h.example.com/download?uuid=1)',   # handler, no suffix
        '[首页](https://h.example.com/)',                      # navigation
    ]
    notice = ('中标（成交）结果公告\n项目编号：X\n采购人：Y\n成交供应商：Z\n' + '\n'.join(links))
    published = {'url': 'https://h.example.com/notice/1', 'source_type': 'tender_document', 'body_text': notice}

    found = attachments(published, model='Q10')
    assert len(found) == 5 > 3                      # the historic cap truncated here
    assert 'https://h.example.com/notice/files/a.pdf' in found
    assert 'https://h.example.com/download?uuid=1' in found
    assert 'https://h.example.com/' not in found

    many = ('中标（成交）结果公告\n项目编号：X\n采购人：Y\n成交供应商：Z\n'
            + '\n'.join(f'[附件{i}](files/{i}.pdf)' for i in range(12)))
    assert len(attachments({**published, 'body_text': many}, model='Q10')) == 10


def test_an_untrusted_page_keeps_the_historic_strict_link_rule():
    """A guess about an untrusted page must not borrow the publisher's rule."""
    body = ('某某产品页 Q10\n'
            '[某某文件](https://h.example.com/download?uuid=1)\n'
            '[报价明细](https://h.example.com/files/d.xlsx)\n')
    page = {'url': 'https://h.example.com/product', 'source_type': 'unknown', 'body_text': body}

    found = attachments(page, model='Q10')
    # The extensionless handler without a document label is not evidence here,
    # even though the same link is accepted on a publication.
    assert found == ['https://h.example.com/files/d.xlsx']
    assert is_document_link('https://h.example.com/download?uuid=1', '', formal=True)
    assert not is_document_link('https://h.example.com/download?uuid=1', '', formal=False)


@pytest.mark.parametrize('url,label,formal,expected', [
    ('https://h.example.com/files/a.xlsx', '', False, True),
    ('https://h.example.com/download?uuid=1', '报价明细', False, True),
    ('https://h.example.com/download?uuid=1', '', False, False),
    ('https://h.example.com/download?uuid=1', '', True, True),
    ('https://h.example.com/?page=2', '', True, False),
    ('https://h.example.com/', '首页', True, False),
])
def test_document_link_shapes(url, label, formal, expected):
    assert is_document_link(url, label, formal=formal) is expected


def test_an_attachment_inherits_a_manufacturer_identity_without_lending_wording(research):  # noqa: F811
    service, run, provider, _ = research
    parent_url = 'https://www.thermofisher.com/product/q10'
    attachment_url = 'https://www.thermofisher.com/product/files/spec.pdf'
    provider.bodies = {
        parent_url: 'Q10 技术规格\n[规格书](files/spec.pdf)',
        attachment_url: 'Q10 容量 120 ml 工作温度 4 ℃',
    }
    parent = {'url': parent_url, 'source_type': 'manufacturer_website', 'body_status': 'fetched',
              'body_text': provider.bodies[parent_url]}
    run['sources'].append(parent)

    service.fetch_attachments(run, parent)
    attachment = next(s for s in run['sources'] if s['url'] == attachment_url)

    # The manufacturer's own document is manufacturer evidence...
    assert attachment['source_type'] == 'manufacturer_website'
    # ...and a manufacturer is not a procurement publisher, so it lends no
    # award wording and its table can never become a price.
    assert 'publication_context' not in attachment
    assert linked_publication(attachment) is None


def test_inheritance_never_overwrites_a_type_the_attachment_proved_itself(research):  # noqa: F811
    service, run, provider, _ = research
    parent_url = 'https://www.thermofisher.com/product/q10'
    attachment_url = 'https://files.example-bid.com/award-0417.pdf'
    provider.bodies = {
        parent_url: f'Q10 技术规格\n[规格书]({attachment_url})',
        attachment_url: AGENCY_NOTICE,
    }
    parent = {'url': parent_url, 'source_type': 'manufacturer_website', 'body_status': 'fetched',
              'body_text': provider.bodies[parent_url]}
    run['sources'].append(parent)

    service.fetch_attachments(run, parent)
    attachment = next(s for s in run['sources'] if s['url'] == attachment_url)

    # Its own bytes are a concluded award notice: inheritance is a default for
    # silence, never a downgrade of established evidence.
    assert attachment['source_type'] == 'tender_document'


# --------------------------------------------------------------------------
# Reverse: nothing below may ever become price evidence.
# --------------------------------------------------------------------------

def test_a_shop_page_cannot_lend_award_wording_to_its_attachment():
    parent_url = 'https://www.example-mall.com/notice/1'
    attachment_url = parent_url + '/files/d.xlsx'
    shop_notice = ('某某仪器商城\n现货 加入购物车\n'
                   '中标（成交）结果公告\n项目编号：X\n采购人：Y\n成交供应商：Z\n')
    # Worst case: the record even claims the publication link.
    attachment = {'url': attachment_url, 'parent_url': parent_url, 'body_status': 'fetched',
                  'source_type': 'tender_document', 'body_text': DETAIL_TABLE,
                  'publication_context': {'url': parent_url, 'body_text': shop_notice,
                                          'linked_url': attachment_url}}

    assert linked_publication(attachment) is None
    assert validate_price(award_row(DETAIL_TABLE), attachment, BASELINE) is None


def test_a_forged_or_missing_parent_identity_lends_nothing():
    parent_url = 'https://www.example-zb.com/notice/20260918-award'
    attachment_url = 'https://files.example.com/detail.xlsx'
    context = {'url': parent_url, 'body_text': AGENCY_NOTICE, 'linked_url': attachment_url}

    def record(**overrides):
        return {'url': attachment_url, 'parent_url': parent_url, 'body_status': 'fetched',
                'source_type': 'tender_document', 'body_text': DETAIL_TABLE,
                'publication_context': context, **overrides}

    assert linked_publication(record()) is not None
    assert linked_publication(record(parent_url='https://elsewhere.example.com/notice')) is None
    assert linked_publication(record(parent_url=None)) is None
    assert linked_publication({k: v for k, v in record().items() if k != 'parent_url'}) is None
    # A record claiming a different file than the one it is must not link.
    assert linked_publication(record(publication_context={**context, 'linked_url': parent_url})) is None


def test_anchors_and_private_network_targets_are_never_attachments():
    notice = ('中标（成交）结果公告\n项目编号：X\n采购人：Y\n成交供应商：Z\n'
              '[本页](https://h.example.com/notice/1#top)\n'
              '[内部](http://127.0.0.1/a.pdf)\n'
              '[内网](http://10.0.0.9/b.pdf)\n'
              '[邮件](mailto:a@b.com)\n'
              '[脚本](javascript:alert(1))\n'
              '[真附件](files/real.pdf)\n')
    published = {'url': 'https://h.example.com/notice/1', 'source_type': 'tender_document', 'body_text': notice}

    assert attachments(published, model='Q10') == ['https://h.example.com/notice/files/real.pdf']
