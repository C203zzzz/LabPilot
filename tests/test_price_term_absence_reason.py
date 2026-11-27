"""A missing commercial term must say *why* it is missing, not just that it is.

The report used to print "配置：待核实" for every absent condition, which reads
the same whether the announcement and its attachment never mention the term or
the extractor read something it could not tie back to their own words. The two
need different answers from a buyer, so the reason is recorded where the source
body is still in hand and shown instead of the shrug.
"""
from backend.services.procurement_research import price_term_text
from backend.services.procurement_rules import validate_price

BODY = '成交公告 2025年6月18日 厂商 X-100 单价6800元 数量2台'
SOURCE = {'url': 'https://example.gov.cn/award-x100', 'body_status': 'fetched',
          'body_text': BODY, 'source_type': 'tender_document'}
BASELINE = {'brand': '厂商', 'model': 'X-100', 'quantity': 1, 'currency': 'CNY'}


def row(**extra):
    raw = {'kind': 'award', 'brand': '厂商', 'model': 'X-100', 'amount': 6800,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 2,
           'excerpt': BODY, 'date': '2025-06-18', 'currency': 'CNY'}
    raw.update(extra)
    return raw


def test_an_absent_term_is_reported_as_not_stated_by_the_announcement():
    checked = validate_price(row(), SOURCE, BASELINE)
    assert checked is not None
    assert 'configuration' in checked['gaps']
    assert checked['field_notes']['configuration'] == '公告正文及已取得的附件未提供该项'


def test_the_absence_note_names_only_the_files_that_were_opened():
    """The note must not assert that a file nobody opened was silent.

    The caller decides which attachments to fold into the body it passes in, so
    from here there is no way to know whether a notice even had attachments.
    "已取得的" is what keeps the sentence true either way, which makes it worth
    pinning rather than leaving to wording drift.
    """
    checked = validate_price(row(), SOURCE, BASELINE)
    note = checked['field_notes']['configuration']
    assert '已取得' in note
    assert '及其附件' not in note


def test_an_attachment_source_gets_the_same_honest_scope():
    attachment = {**SOURCE, 'url': 'https://example.gov.cn/award-x100.pdf',
                  'parent_url': SOURCE['url']}
    checked = validate_price(row(), attachment, BASELINE)
    assert checked is not None
    assert checked['field_notes']['configuration'] == '公告正文及已取得的附件未提供该项'


def test_a_stated_configuration_keeps_its_value_and_gets_no_absence_note():
    body = BODY + ' 配置：含6×50ml角转子'
    source = {**SOURCE, 'body_text': body}
    checked = validate_price(row(configuration='含6×50ml角转子'), source, BASELINE)
    assert checked is not None
    assert checked['configuration'] == '含6×50ml角转子'
    assert 'configuration' not in checked['field_notes']
    assert 'configuration' not in checked['gaps']


def test_a_configuration_that_cannot_be_tied_to_the_text_says_exactly_that():
    checked = validate_price(row(configuration='含8×50ml角转子（进口）'), SOURCE, BASELINE)
    assert checked is not None
    assert checked['configuration'] == 'unknown'
    assert checked['field_notes']['configuration'] == (
        '抽取结果无法与公告正文及已取得的附件的原文逐字对应，已按未提供处理')


def test_every_business_term_gets_a_reason_not_just_configuration():
    checked = validate_price(row(), SOURCE, BASELINE)
    assert set(checked['field_notes']) >= {'configuration', 'tax', 'installation', 'warranty', 'delivery'}


def test_the_report_shows_the_reason_instead_of_a_shrug():
    absent = {'configuration': 'unknown',
              'field_notes': {'configuration': '公告正文及已取得的附件未提供该项'}}
    assert price_term_text(absent, 'configuration') == '公告正文及已取得的附件未提供该项'
    assert price_term_text({'configuration': '含6×50ml角转子'}, 'configuration') == '含6×50ml角转子'
    # A record stored before the reason was recorded still renders.
    # 兜底也不写"待核实"（设计规范：这类字样让结果显得不可信），
    # 直接说清缺在哪：公告里没写。
    assert price_term_text({'configuration': None}, 'configuration') == '公告里没写'
