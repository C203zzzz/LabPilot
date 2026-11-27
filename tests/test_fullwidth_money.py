# -*- coding: utf-8 -*-
"""What OCR actually reports for an amount: ７４，８００, not 74,800.

Phase 3 made the structured chain run for scanned pages, so OCR text now
reaches the price lane for the first time. It arrives full-width, and the
readers disagree about what that means::

    parse_amount('74,800')  -> 74800.0
    parse_amount('７４８００') -> 74800.0     # \\d and float() accept full-width digits
    parse_amount('74，800')  -> None         # the full-width comma defeats it

The prose patterns are worse than the table one: ``\\d[\\d,]*`` over
``单价 74，800 元`` matches ``74`` and ``800`` separately, so a full-width
separator yields a *wrong* number rather than a missing one. The fold happens
where text enters the pipeline, so the money readers themselves stay untouched
(handoff section 6 forbids rewriting them) and the snapshot keeps the bytes the
publisher actually served.
"""
import re
from pathlib import Path

import pytest

from backend.infrastructure.search_providers import PublicWebSearchProvider
from backend.infrastructure.unicode_text import fold_width
from backend.services.procurement_tables import price_table_rows
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FULLWIDTH_TABLE = (
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 甲厂Q１０ | 2 | ７４，８００\n'
)


class _Reader(PublicWebSearchProvider):
    def __init__(self, body, content_type='text/html; charset=utf-8'):
        self.body = body.encode('utf-8')
        self.content_type = content_type

    def fetch_raw(self, url, timeout):
        return self.body, self.content_type


def test_only_the_separator_between_digits_is_folded():
    assert fold_width('单价７４，８００元') == '单价７４,８００元'
    assert fold_width('单价 74，800 元') == '单价 74,800 元'
    assert fold_width('７４．５') == '７４.５'          # the separator moves, the ５ does not
    assert fold_width('') == ''
    assert fold_width('Q10 | 74,800') == 'Q10 | 74,800'            # idempotent
    # The full-width comma is ordinary sentence punctuation, and it must stay
    # exactly as published. Folding it globally rewrote notices like this one.
    assert fold_width('Q10 成交公告，预算 12000 元') == 'Q10 成交公告，预算 12000 元'
    assert fold_width('附件：报价明细（2026年），共２包') == '附件：报价明细（2026年），共２包'
    assert fold_width('（元）：７４，５００ 元，含税') == '（元）：７４,５００ 元，含税'


def test_the_fold_cannot_invent_a_number_that_was_not_there():
    # NFKC would turn these into 1 / 2 / 株式会社, and a global digit map would
    # rewrite ２０２６年. A price reader must never see a digit the document did
    # not print, and a notice must keep the width it was published in.
    assert fold_width('① ② ㈱ Ⅳ') == '① ② ㈱ Ⅳ'
    assert fold_width('２０２６年９月２２日') == '２０２６年９月２２日'
    assert not re.search(r'\d', fold_width('① ②'))


def test_a_full_width_amount_reaches_the_price_lane_as_one_number(research):  # noqa: F811
    service, run, _, _ = research
    service.search.providers = [_Reader(FULLWIDTH_TABLE)]
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/scan.htm', 'source_type': 'unknown'})

    assert source['body_status'] == 'fetched'
    assert '７４,８００' in source['body_text']          # only the separator moved
    rows = price_table_rows(source['body_text'], 'Q10')
    assert [row['amount'] for row in rows] == [74800]
    assert rows[0]['amount_scope'] == 'unit' and rows[0]['quantity'] == 2
    # Causal control: the same row, unread, is the defect this fold removes.
    assert price_table_rows(FULLWIDTH_TABLE, 'Q10') == []
    # ... and the prose lane would have quoted two numbers instead of one.
    assert re.findall(r'\d[\d,]*', '单价 ７４，８００ 元') == ['７４', '８００']
    assert re.findall(r'\d[\d,]*', fold_width('单价 ７４，８００ 元')) == ['７４,８００']


def test_the_fold_is_recorded_and_the_snapshot_stays_verbatim(research):  # noqa: F811
    service, run, _, _ = research
    service.search.providers = [_Reader(FULLWIDTH_TABLE)]
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/verbatim.htm', 'source_type': 'unknown'})

    warning = next(item for item in source['text_warnings'] if item['code'] == 'fullwidth_folded')
    assert warning['count'] == 1                      # one separator, nothing else
    assert '全角' in warning['message']
    # A citation may only be checked against the document as published.
    snapshot = Path(service.repo.path).parent / 'research-snapshots' / (source['snapshot']['sha256'] + '.bin')
    assert '７４，８００'.encode('utf-8') in snapshot.read_bytes()
    assert '７４,８００' not in snapshot.read_text(encoding='utf-8')


def test_half_width_text_records_no_fold_warning(research):  # noqa: F811
    service, run, _, _ = research
    service.search.providers = [_Reader('货物名称 | 规格型号 | 数量 | 单价（元）\n生物安全柜 | 甲厂Q10 | 2 | 74,800\n')]
    source = service.fetch_source(run, {'url': 'https://buyer.gov.cn/notice/clean.htm', 'source_type': 'unknown'})
    assert not [item for item in source.get('text_warnings') or [] if item['code'] == 'fullwidth_folded']
    assert [row['amount'] for row in price_table_rows(source['body_text'], 'Q10')] == [74800]


@pytest.mark.parametrize('text', ['货物名称 | 规格型号 | 数量 | 单价（元）', '附件：报价明细（2026年）'])
def test_the_fold_never_rewrites_notice_punctuation(text):
    # Excerpts of ordinary notices must stay byte-identical to the source.
    assert fold_width(text) == text
