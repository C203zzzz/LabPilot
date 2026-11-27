# -*- coding: utf-8 -*-
"""Step two of the chain: 拿到 -- a price table can also be an *image*.

``search_providers`` used to decide "the PDF was read" by asking "did the
parser raise?".  On a scanned award notice the answer is no: ``pdfplumber``
returns the header and footer of the text pages, the price table stays a
picture, and the source is recorded as a clean ``fetched`` body that contains
no price at all.  The trigger is wrong, not the parser.

The fix judges the *result*: the decoder now reports what it reached (how many
pages carried characters, how many carried only an image), and the structured
chain runs whenever that result is demonstrably incomplete.  The fixtures below
build real PDFs with a text page and an image-only page, so these tests fail
against the old trigger for the real reason.
"""
import hashlib

import pytest

from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.services.procurement_research import ProcurementResearch
from backend.services.procurement_tables import price_table_rows
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

NOTICE_LINE = '某某市第一人民医院医疗设备采购项目中标（成交）结果公告'
# Only OCR can reach this row: it lives on the image-only page.
SCANNED_TABLE = (
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 甲厂Q10 | 2 | 74,800\n'
)


def _paged_pdf(pages):
    """Build a PDF from page specs: ``('text', str)`` or ``('scan',)``."""
    import fitz

    pdf = fitz.open()
    for kind, *rest in pages:
        page = pdf.new_page(width=595, height=842)
        if kind == 'scan':
            # A page that exists only as pixels -- what a scanner produces and
            # what text extraction cannot reach.
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
            pixmap.set_rect(pixmap.irect, (255, 255, 255))
            page.insert_image(fitz.Rect(40, 80, 240, 280), stream=pixmap.tobytes('png'))
        else:
            page.insert_text((40, 60), rest[0], fontname='china-s', fontsize=11)
    payload = pdf.tobytes()
    pdf.close()
    return payload


class _Reader(PublicWebSearchProvider):
    """A fetcher that only supplies bytes; the real PDF decoding runs."""

    def __init__(self, payload, content_type='application/pdf'):
        self.payload = payload
        self.content_type = content_type

    def fetch_raw(self, url, timeout):
        return self.payload, self.content_type


def test_report_separates_a_scanned_page_from_a_text_page():
    payload = _paged_pdf([('text', NOTICE_LINE), ('scan',)])
    text, report = PublicWebSearchProvider.decode_body_with_report(payload, 'application/pdf')
    assert report['pages'] == 2
    assert report['pages_with_text'] == 1
    assert report['image_only_pages'] == 1
    assert report['incomplete'] is True
    assert NOTICE_LINE in text
    # The old entry point keeps its old contract exactly: one text value.
    assert text == PublicWebSearchProvider.decode_body(payload, 'application/pdf')

    clean = _paged_pdf([('text', NOTICE_LINE)])
    _, clean_report = PublicWebSearchProvider.decode_body_with_report(clean, 'application/pdf')
    assert clean_report['incomplete'] is False
    assert clean_report['image_only_pages'] == 0
    assert clean_report['pages'] == clean_report['pages_with_text'] == 1


def test_an_image_only_page_escalates_to_the_structured_chain(research, monkeypatch):  # noqa: F811
    service, run, _, _ = research
    payload = _paged_pdf([('text', NOTICE_LINE), ('scan',)])
    service.search.providers = [_Reader(payload)]
    escalated = []

    def structured(self, run_, source, payload_):
        escalated.append(source['parse_report'])
        return SCANNED_TABLE, ''        # what OCR and layout analysis add

    monkeypatch.setattr(ProcurementResearch, 'structured_pdf_text', structured)
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/scan.pdf', 'source_type': 'unknown'})

    # The escalation ran, and it ran with the evidence that it was needed.
    assert escalated and escalated[0]['image_only_pages'] == 1
    assert '生物安全柜' in source['body_text']
    assert source['body_status'] == 'fetched'
    # Causal control: the row is only reachable *because* the scan was read.
    # The weak text alone has no price, which is the defect this phase fixes.
    assert price_table_rows(NOTICE_LINE, 'Q10') == []
    rows = price_table_rows(source['body_text'], 'Q10')
    assert [row['amount'] for row in rows] == [74800]
    assert rows[0]['amount_scope'] == 'unit'


def test_a_fully_readable_pdf_never_invokes_the_structured_chain(research, monkeypatch):  # noqa: F811
    service, run, _, _ = research
    payload = _paged_pdf([('text', NOTICE_LINE + ' 甲厂Q10 单价 74,800 元')])
    service.search.providers = [_Reader(payload)]

    def structured(self, run_, source, payload_):  # pragma: no cover - must not run
        raise AssertionError('a complete text layer must not pay for OCR')

    monkeypatch.setattr(ProcurementResearch, 'structured_pdf_text', structured)
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/text.pdf', 'source_type': 'unknown'})
    assert source['body_status'] == 'fetched'
    assert '74,800' in source['body_text']
    assert source['parse_report']['incomplete'] is False


def test_a_degraded_structured_parse_cannot_overwrite_clean_text(research, monkeypatch):  # noqa: F811
    service, run, _, _ = research
    payload = _paged_pdf([('text', NOTICE_LINE), ('scan',)])
    service.search.providers = [_Reader(payload)]
    gap = 'PDF已保存但解析链报告错误，未将缺失页面或字段当作正文事实。'
    monkeypatch.setattr(ProcurementResearch, 'structured_pdf_text',
                        lambda self, run_, source, payload_: ('<损坏的中间产物>', gap))
    run.setdefault('gaps', [])
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/partial.pdf', 'source_type': 'unknown'})

    # A parser that reports errors produced text we cannot trust, so the clean
    # weak text survives and the degradation stays visible.
    assert source['body_status'] == 'fetched'
    assert NOTICE_LINE in source['body_text']
    assert '损坏的中间产物' not in source['body_text']
    assert source['parse_status'] == 'partial'
    assert source['gap_reason'] == gap
    assert any('解析链报告错误' in item for item in run['gaps'])


def test_a_fetcher_with_its_own_decoder_is_not_overridden_by_the_report(research):  # noqa: F811
    service, run, _, _ = research

    class Custom(PublicWebSearchProvider):
        """Overrides only ``decode_body``; the report is inherited."""

        def fetch_raw(self, url, timeout):
            return b'%PDF not really a pdf', 'application/pdf'

        def decode_body(self, payload, content_type, url=''):
            return '自有解码 甲厂Q10 单价 74,800 元'

    service.search.providers = [Custom()]
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/custom.pdf', 'source_type': 'unknown'})
    # The inherited report would have decoded the same bytes a second, different
    # way and destroyed this fetcher's answer.
    assert source['body_status'] == 'fetched'
    assert source['body_text'] == '自有解码 甲厂Q10 单价 74,800 元'


def test_the_parse_report_reaches_the_saved_record(research):  # noqa: F811
    service, run, _, _ = research
    payload = _paged_pdf([('text', NOTICE_LINE), ('scan',)])
    service.search.providers = [_Reader(payload)]
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/reported.pdf', 'source_type': 'unknown'})
    key = 'body-cache-' + hashlib.sha256(source['url'].encode()).hexdigest()
    cached = next(record for record in service.records(run['task_id'], 'body_cache') if record['id'] == key)
    assert source['parse_report']['image_only_pages'] == 1
    # Degradation that is not visible cannot be reviewed or repaired.
    assert cached['body']['parse_report']['image_only_pages'] == 1


def test_pdf_page_limit_still_asks_for_specific_pages():
    payload = _paged_pdf([('text', NOTICE_LINE)] * 61)
    with pytest.raises(ValueError, match='明细页'):
        PublicWebSearchProvider.decode_body_with_report(payload, 'application/pdf')


def test_blank_local_chain_text_does_not_mask_the_reason_it_failed(research, monkeypatch):  # noqa: F811
    """两页空白拼成的 ``'\\n\\n'`` 曾经被当成"读到了正文"。

    ``ingest_file`` 对一份两页都读不出字的 PDF 返回的是 ``'\\n\\n'``：**真值成立、
    内容为空**。``_fetch_source`` 用 ``elif text:`` 判真值，于是 weak 解析器的
    失败被这个空白串顶掉，报出去的原因是「来源正文为空」——读起来像站点给了空
    正文，而链上真正记下的原因（本机没有本地识别组件）只留在
    ``text_provenance.warnings`` 里。实测代价见 ``设计规范``：一个纯扫描件的
    分项报价表两次都读成空，报告把「本机缺 OCR 组件」说成了「来源正文为空」。
    """
    service, run, _, _ = research
    service.search.providers = [_Reader(_paged_pdf([('scan',), ('scan',)]))]

    def structured(self, run_, source, payload_):
        source['text_provenance'] = {
            'method': 'local_pdf_parser',
            'warnings': [
                {'code': 'structured_parser_failed', 'message': '结构化解析未完成',
                 'severity': 'warning'},
                {'code': 'local_ocr_unavailable',
                 'message': "本地轻量识别引擎不可用：ModuleNotFoundError: No module named 'rapidocr'",
                 'severity': 'warning'},
            ]}
        return '\n\n', ''      # two image-only pages, joined

    monkeypatch.setattr(ProcurementResearch, 'structured_pdf_text', structured)
    run.setdefault('gaps', [])
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/scan-only.pdf',
                                        'source_type': 'unknown'})

    assert source['body_status'] == 'failed'
    assert not (source.get('body_text') or '').strip()
    # 报出的原因必须是链上记下的那一条，不是「来源正文为空」。
    assert source['failure_kind'] != 'empty_body'
    blockers = [item for item in run['gaps'] if '本机没有可用的本地识别组件' in item]
    assert blockers, run['gaps']
    assert not any('来源正文为空' in item for item in run['gaps'])
    # 机器细节留在记录里，句子只说"本机缺什么"。
    assert 'rapidocr' not in blockers[0]
