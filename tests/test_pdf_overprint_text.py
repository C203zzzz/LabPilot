"""A PDF that fakes bold by printing each glyph twice must not double our text.

Observed on real award notices (内蒙古科技大学, ccgp-neimenggu.gov.cn): the PDF
draws the heading a second time 0.42pt to the left. PyMuPDF's text pass, pdfium
and docling's layout pass all merge the twin, but pdfplumber reports both
drawings, so ``_pdf_tables_text`` turned 品目 into 品品目目 and no price label
matched any more. These tests pin the merge and the fixture that proves it is
needed.
"""
from __future__ import annotations

import io

import fitz
import pdfplumber

from backend.infrastructure.search_providers import (
    PublicWebSearchProvider,
    collapse_overprinted_chars,
)


def overprinted_pdf(text: str = "PRICE", offset: float = 0.5) -> bytes:
    """A one-page PDF with *text* drawn twice, the fake-bold shape."""
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 100), text, fontsize=14)
    page.insert_text((72 + offset, 100), text, fontsize=14)
    payload = pdf.tobytes()
    pdf.close()
    return payload


def single_print_pdf(text: str = "PRICE") -> bytes:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 100), text, fontsize=14)
    payload = pdf.tobytes()
    pdf.close()
    return payload


def test_the_fixture_really_carries_the_overprint():
    """Without this, the merge test below could pass on a fixture that never
    reproduced the defect in the first place."""
    with pdfplumber.open(io.BytesIO(overprinted_pdf())) as document:
        assert "PPRRIICCEE" in (document.pages[0].extract_text() or "")


def test_overprinted_glyphs_are_read_once():
    body, report = PublicWebSearchProvider._pdf_tables_text(overprinted_pdf())
    assert "PPRRIICCEE" not in body
    assert "PRICE" in body
    assert report["pages"] == 1 and report["characters"] > 0


def test_a_page_without_overprint_is_returned_untouched():
    with pdfplumber.open(io.BytesIO(single_print_pdf())) as document:
        page = document.pages[0]
        assert collapse_overprinted_chars(page) is page


def test_deduped_page_keeps_its_table():
    """The merge must not cost the table detector its table."""
    pdf = fitz.open()
    page = pdf.new_page()
    for column in range(3):
        x = 72 + column * 90
        for row in range(3):
            y = 100 + row * 20
            page.insert_text((x, y), "CELL")
            page.insert_text((x + 0.5, y), "CELL")
    page.draw_line((60, 90), (350, 90))
    page.draw_line((60, 90), (60, 160))
    page.draw_line((350, 90), (350, 160))
    page.draw_line((60, 160), (350, 160))
    for column in range(2):
        page.draw_line((60 + (column + 1) * 90, 90), (60 + (column + 1) * 90, 160))
    payload = pdf.tobytes()
    pdf.close()
    with pdfplumber.open(io.BytesIO(payload)) as document:
        plain_page = document.pages[0]
        assert len(collapse_overprinted_chars(plain_page).find_tables()) == len(plain_page.find_tables())
    body, _ = PublicWebSearchProvider._pdf_tables_text(payload)
    assert "CCEELLLL" not in body
