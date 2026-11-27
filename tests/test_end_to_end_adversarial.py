# -*- coding: utf-8 -*-
"""Phase 9: the four steps on one run, with the negatives that cost money.

Every earlier phase proved its own link. This file asks what the links are for:
given bytes from a real award notice, does the price lane end up with a
*traceable* unit price for the model the user asked about, and does it refuse
the things that look like a price but are not one?

The corpus is deliberately shaped so only the whole chain can answer:

* the price lives in a **scanned attachment** of an agency's (``*.com``) award
  notice, so it needs 找到 (publication accepted) -> 拿到 (attachment linked and
  decoded whole) -> 认出 (OCR text, and a **full-width** amount) to be reachable
  at all;
* a cancelled ("废标") notice, a manufacturer page and an unrelated stationery
  award all publish numbers that must not become this model's deal price;
* the same corpus is asked about two models at once, because a body cache
  keyed by URL is exactly where one model's answer could leak into another's.
"""
import copy
import threading
from dataclasses import dataclass, field

import pytest

from backend.infrastructure.search_providers import PublicWebSearchProvider
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

AGENCY = 'https://www.zb-agent.com'
NOTICE = AGENCY + '/notice/2026-09-11'
DETAIL = AGENCY + '/notice/2026-09-11/files/detail.pdf'
# The cancelled notice is published on a real government-procurement host and
# carries the two structural fields (项目编号 + 采购人) that make
# ``source_authority.is_tender`` accept it as a ``tender_document``. It used to
# sit on the agency's ``*.com`` host with no such fields, so it stopped at
# ``source_type='unknown'`` and ``prices()`` dropped it at the **source
# whitelist** -- before any price predicate ran. The refusal it asserted was
# therefore never produced by the price gate, and deleting the whole 废标 rule
# from ``validate_price`` left the test green. It now reaches the gate.
CANCELLED_HOST = 'https://ggzyjyw.hlj.gov.cn'
CANCELLED = CANCELLED_HOST + '/zbgg/2026/cancelled-0920.html'
VENDOR = 'https://www.acme-lab.com/q10'
OFFTOPIC = AGENCY + '/notice/2026-08-01'

NOTICE_HTML = (
    '<html><head><title>某某市第一人民医院生物安全柜采购项目中标（成交）结果公告</title></head><body>'
    '<h1>某某市第一人民医院生物安全柜采购项目中标（成交）结果公告</h1>'
    '<p>采购人：某某市第一人民医院　采购代理机构：某某招标代理有限公司</p>'
    '<p>中标（成交）供应商：某某医疗器械有限公司</p>'
    '<p>公告日期：2026-09-11</p>'
    '<p>附件：<a href="' + DETAIL + '">报价明细</a></p>'
    '</body></html>'
)
# The row the scanned page carries. Full-width digits and a full-width
# thousands separator: what OCR of a Chinese price table actually produces.
SCANNED_TABLE = (
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 甲厂Q10 | 2 | ７４，８００\n'
)
CANCELLED_HTML = (
    '<html><head><title>某某市第一人民医院生物安全柜采购项目废标公告</title></head><body>'
    '<h1>生物安全柜采购项目废标公告</h1>'
    '<p>项目编号：HLJ-2026-0920　采购人：某某市第一人民医院</p>'
    '<p>因有效投标人不足三家，本项目废标，采购活动终止。</p>'
    '<p>原中标候选人报价如下（已废标，仅供参考）：</p>'
    '<p>货物名称 | 规格型号 | 数量 | 单价（元）</p>'
    '<p>生物安全柜 | 甲厂Q10 | 2 | 74,800</p>'
    '<p>公告日期：2026-09-20</p>'
    '</body></html>'
)
VENDOR_HTML = (
    '<html><head><title>甲厂Q10 生物安全柜 产品页</title></head><body>'
    '<h1>甲厂Q10 生物安全柜</h1><p>参考价 74,800 元，具体价格面议。</p>'
    '</body></html>'
)
OFFTOPIC_HTML = (
    '<html><head><title>某某中学办公用品采购项目中标（成交）结果公告</title></head><body>'
    '<h1>某某中学办公用品采购项目中标（成交）结果公告</h1>'
    '<p>中标供应商：某某文具店　公告日期：2026-08-01</p>'
    '<p>货物名称 | 规格型号 | 数量 | 单价（元）</p>'
    '<p>打印纸 | A4 70g | 100 | 25</p>'
    '</body></html>'
)


def _paged_pdf(pages):
    """A real PDF: ``('text', str)`` pages and ``('scan',)`` image-only pages."""
    import fitz

    pdf = fitz.open()
    for kind, *rest in pages:
        page = pdf.new_page(width=595, height=842)
        if kind == 'scan':
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
            pixmap.set_rect(pixmap.irect, (255, 255, 255))
            page.insert_image(fitz.Rect(40, 80, 240, 280), stream=pixmap.tobytes('png'))
        else:
            page.insert_text((40, 60), rest[0], fontname='china-s', fontsize=11)
    payload = pdf.tobytes()
    pdf.close()
    return payload


@dataclass
class _Corpus(PublicWebSearchProvider):
    """Serves the corpus' bytes; every decoder below it stays real.

    ``pages`` is a *dataclass field*, not a plain ``__init__`` argument. The
    provider it subclasses is a dataclass, and ``for_referer`` -- which the
    attachment read uses to send the notice as ``Referer`` -- returns a
    ``dataclasses.replace`` view of whatever provider it is called on. That
    view is rebuilt from the class' fields, so a subclass that replaces
    ``__init__`` with a signature the fields do not fit raises ``TypeError``
    inside the attachment step, and the whole corpus loses its attachment.
    """

    pages: dict = field(default_factory=dict)

    def fetch_raw(self, url, timeout):
        return self.pages[url]


@pytest.fixture
def corpus(research, monkeypatch):  # noqa: F811
    """Fetch the whole corpus into one run and hand back the run."""
    service, run, _, _ = research
    pages = {
        NOTICE: (NOTICE_HTML.encode('utf-8'), 'text/html; charset=utf-8'),
        DETAIL: (_paged_pdf([('text', '某某市第一人民医院生物安全柜采购项目中标（成交）结果公告 公告日期：2026-09-11'), ('scan',)]),
                 'application/pdf'),
        CANCELLED: (CANCELLED_HTML.encode('utf-8'), 'text/html; charset=utf-8'),
        VENDOR: (VENDOR_HTML.encode('utf-8'), 'text/html; charset=utf-8'),
        OFFTOPIC: (OFFTOPIC_HTML.encode('utf-8'), 'text/html; charset=utf-8'),
    }
    service.search.providers = [_Corpus(pages=pages)]

    def structured(self, run_, source, payload):
        return SCANNED_TABLE, ''          # what OCR and layout analysis add

    monkeypatch.setattr(type(service), 'structured_pdf_text', structured)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    # ``fetch_source`` returns the record; the search loop is what files it, so
    # this fixture does the same filing for the corpus.
    for url in (NOTICE, CANCELLED, VENDOR, OFFTOPIC):
        run['sources'].append(service.fetch_source(run, {'url': url, 'source_type': 'unknown'}))
    notice = next(s for s in run['sources'] if s['url'] == NOTICE)
    service.fetch_attachments(run, notice)
    return service, run


def _price_rows(service, run):
    from backend.services.price_leads import price_leads
    prices = service.prices(run, run['baseline'])
    run['price_leads'] = price_leads(run['sources'], run['baseline'])
    return prices


def test_the_chain_returns_a_traceable_unit_price_from_a_scanned_attachment(corpus):
    service, run = corpus
    attachment = next(s for s in run['sources'] if s['url'] == DETAIL)
    # 拿到: the attachment was reached, linked to its parent, and read whole.
    assert attachment['body_status'] == 'fetched'
    assert attachment['source_type'] == 'tender_document'
    assert attachment['parse_report']['image_only_pages'] == 1     # OCR escalation was justified
    # 认出: the OCR'd separator was folded at the entry, visibly. Full-width
    # *digits* stay as published -- the readers already accept them.
    assert '７４,８００' in attachment['body_text'] and '７４，８００' not in attachment['body_text']
    assert any(w.get('code') == 'fullwidth_folded' for w in attachment.get('text_warnings', []))

    prices = _price_rows(service, run)
    assert [p['unit_price'] for p in prices] == [74800]
    price = prices[0]
    # Every claim is bound to a source, a line of it, and a date.
    assert price['url'] == DETAIL
    assert '７４,８００' in price['excerpt'] and price['excerpt'] in attachment['body_text']
    assert price['evidence_location']['char_start'] == attachment['body_text'].find(price['excerpt'])
    assert price['evidence_location']['snapshot']['sha256'] == attachment['snapshot']['sha256']
    assert price['date'] == '2026-09-11' and price['date_verified'] is True
    assert price['model'] == 'Q10' and price['kind'] == 'award'


def test_no_other_document_in_the_corpus_becomes_a_deal_price(corpus):
    """A vendor page, a stationery award and a cancelled notice are not prices."""
    service, run = corpus
    prices = _price_rows(service, run)
    assert {p['url'] for p in prices} == {DETAIL}
    vendor = next(s for s in run['sources'] if s['url'] == VENDOR)
    # The manufacturer page is not a publication, so the award gate cannot use
    # its number even when a publisher pass would later label it.
    assert vendor['source_type'] != 'tender_document'
    assert all(p['level'] != 'current_comparable' for p in prices if p['url'] == VENDOR)


def test_a_cancelled_notices_price_is_refused(corpus):
    """废标 wording must not keep a dead price alive.

    The reverse assertion matters more than the positive one: the cancelled
    notice carries the same model, the same amount and a table-shaped row, so a
    refusal cannot be an accident of the text never having been readable.

    The cancelled notice is also asserted to **be** a ``tender_document``. That
    is the assertion this test used to lack: while it sat on the agency's
    ``*.com`` host without structural fields, ``source_type`` stayed ``unknown``
    and the source whitelist in ``prices()`` -- not the price gate -- produced
    the refusal. Now the 废标 rule inside ``validate_price`` is the only thing
    standing between this row and the result.
    """
    service, run = corpus
    cancelled = next(s for s in run['sources'] if s['url'] == CANCELLED)
    # 走到价格判定：来源必须先被接受为公告。
    assert cancelled['source_type'] == 'tender_document', cancelled.get('source_type')
    from backend.services.procurement_tables import price_table_rows
    assert price_table_rows(cancelled['body_text'], 'Q10'), cancelled['body_text']
    prices = _price_rows(service, run)
    assert CANCELLED not in {p['url'] for p in prices}


def test_an_image_the_ocr_reads_as_empty_is_not_reported_as_an_empty_page(tmp_path):
    """A picture with no readable text is a *failed* read, never "no content".

    The distinction is the whole reason this row exists: "the page carries no
    information" and "we could not read the page" lead to different repairs, so
    an empty OCR result has to stay visible as a failure with a warning.
    """
    from types import SimpleNamespace

    from backend.schemas.documents import DocumentImage, DocumentRecord
    from backend.services.document_ingestion import DocumentIngestionService
    from backend.services.vision import VisionResult

    ingestion = DocumentIngestionService(tmp_path / 'files')
    document = DocumentRecord('d', 'scan.pdf', '', 'pdf', 'application/pdf', None, None, None)
    image = DocumentImage(1, 'page:1/rendered', 'image/png')
    document.images.append(image)

    ingestion._apply_vision(document, image, VisionResult(text='', fields={}),
                            adapter=SimpleNamespace(provider='fake'))
    assert image.ocr_status == 'failed'
    assert document.ocr_status == 'failed'
    assert [w.code for w in document.warnings] == ['ocr_empty_result']
    assert '空文本' in document.warnings[0].message


def test_two_models_asked_at_once_do_not_share_an_answer(research, monkeypatch):  # noqa: F811
    """The body cache is keyed by URL, so this is where a leak would show up."""
    service, run, _, _ = research
    pages = {
        NOTICE: (NOTICE_HTML.encode('utf-8'), 'text/html; charset=utf-8'),
        DETAIL: (_paged_pdf([('text', '某某市第一人民医院生物安全柜采购项目中标（成交）结果公告 公告日期：2026-09-11'), ('scan',)]),
                 'application/pdf'),
    }
    service.search.providers = [_Corpus(pages=pages)]
    monkeypatch.setattr(type(service), 'structured_pdf_text',
                        lambda self, run_, source, payload: (SCANNED_TABLE, ''))
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    for url in (NOTICE, DETAIL):
        run['sources'].append(service.fetch_source(run, {'url': url, 'source_type': 'unknown'}))
    service.fetch_attachments(run, next(s for s in run['sources'] if s['url'] == NOTICE))

    other = copy.deepcopy(run)
    other.update(id=run['id'] + '-x200', prices=[], price_leads=[], gaps=[],
                 baseline={**run['baseline'], 'model': 'X200', 'brand': '乙厂'})
    results, failures = {}, []

    def measure(key, target_run):
        try:
            results[key] = service.prices(target_run, target_run['baseline'])
        except Exception as exc:                      # pragma: no cover - reported below
            failures.append((key, exc))

    threads = [threading.Thread(target=measure, args=(key, target)) for key, target in
               (('Q10', run), ('X200', other))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    # The same bytes answer both questions, and each answer belongs to one model.
    assert [p['unit_price'] for p in results['Q10']] == [74800]
    assert all(p['model'] == 'Q10' for p in results['Q10'])
    assert results['X200'] == []                      # no price invented for the other model
    assert all(p['url'] == DETAIL for p in results['Q10'])
