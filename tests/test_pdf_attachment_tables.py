# -*- coding: utf-8 -*-
"""PDF attachment table decoding and ccgp attachment link extraction."""
import pytest

from backend.infrastructure.search_providers import PublicWebSearchProvider


def _grid_pdf(rows, narrative=None, width=760, height=420):
    """Build a ruled-table PDF the way procurement attachments lay out items."""
    import fitz

    pdf = fitz.open()
    page = pdf.new_page(width=width, height=height)
    cols = max(len(row) for row in rows)
    left, top, cell_w, cell_h = 40, 80, 150, 40
    for column in range(cols + 1):
        x = left + column * cell_w
        page.draw_line((x, top), (x, top + len(rows) * cell_h))
    for row in range(len(rows) + 1):
        y = top + row * cell_h
        page.draw_line((left, y), (left + cols * cell_w, y))
    if narrative:
        page.insert_text((left, 40), narrative, fontname="china-s", fontsize=11)
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            page.insert_text(
                (left + column_index * cell_w + 5, top + row_index * cell_h + 27),
                value, fontname="china-s", fontsize=10,
            )
    payload = pdf.tobytes()
    pdf.close()
    return payload


def test_pdf_table_decodes_to_pipe_rows_and_feeds_price_extraction():
    payload = _grid_pdf([
        ["型号", "单价(元)", "数量"],
        ["LA2-4A2", "48000", "2"],
    ], narrative="中标明细")
    body = PublicWebSearchProvider.decode_body(payload, "application/pdf")
    assert "型号 | 单价(元) | 数量" in body
    assert "LA2-4A2 | 48000 | 2" in body
    assert "中标明细" in body  # narrative context outside tables is kept

    from backend.services.procurement_tables import price_table_rows
    rows = price_table_rows(body, "LA2-4A2")
    assert len(rows) == 1
    assert rows[0]["amount"] == 48000
    assert rows[0]["amount_scope"] == "unit"
    assert rows[0]["quantity"] == 2


def test_pdf_narrative_lines_do_not_leak_into_table_rows():
    payload = _grid_pdf([
        ["型号", "单价(元)"],
        ["HR40-IIA", "36000"],
    ], narrative="总中标金额叁万陆仟元整")
    body = PublicWebSearchProvider.decode_body(payload, "application/pdf")
    table_lines = [line for line in body.splitlines() if "|" in line]
    assert table_lines == ["型号 | 单价(元)", "HR40-IIA | 36000"]
    assert any("总中标金额" in line and "|" not in line for line in body.splitlines())


def test_textless_pdf_reports_ocr_requirement():
    import fitz

    pdf = fitz.open()
    pdf.new_page()
    payload = pdf.tobytes()
    pdf.close()
    with pytest.raises(ValueError, match="请补充OCR"):
        PublicWebSearchProvider.decode_body(payload, "application/pdf")


def test_oversized_pdf_requires_specific_pages():
    import fitz

    pdf = fitz.open()
    for _ in range(61):
        pdf.new_page()
    payload = pdf.tobytes()
    pdf.close()
    with pytest.raises(ValueError, match="明细页"):
        PublicWebSearchProvider.decode_body(payload, "application/pdf")


def test_broken_pdf_reports_unreadable_body():
    with pytest.raises(ValueError, match="PDF正文无法解析"):
        PublicWebSearchProvider.decode_body(b"%PDF broken", "application/pdf")


def test_fujian_inline_award_line_items_extract_unit_prices():
    """Real Fujian ccgp result bodies embed full quote-detail tables inline."""
    from backend.services.procurement_tables import price_table_rows

    body = "\n".join([
        "四、主要标的信息",
        "合同包6:",
        "货物类（福建蓬瑞科技有限公司）",
        "品目号 | 品目名称 | 采购标的 | 报价明细内容 | 品牌 | 规格型号 | 数量（单位） | 单价(元) | 总价(元)",
        "6-1 | 病房护理及医院设备 | 输液泵 | 输液泵 | 迈瑞 | BeneFusion zVP ex | 30.00(台) | 3,000.00 | 90,000.00",
    ])
    rows = price_table_rows(body, "BeneFusion zVP ex")
    assert len(rows) == 1
    assert rows[0]["amount"] == 3000
    assert rows[0]["quantity"] == 30
    assert rows[0]["brand"] == "迈瑞"
    assert rows[0]["amount_scope"] == "unit"
    assert "6-1" in rows[0]["excerpt"]
    assert not price_table_rows(body, "MT1")


def test_quantity_without_parenthesized_unit_still_accepted():
    from backend.services.procurement_tables import price_table_rows

    body = "\n".join([
        "型号 | 数量 | 单价(元)",
        "SPX-150BIII | 1 | 6800",
    ])
    rows = price_table_rows(body, "SPX-150BIII")
    assert rows[0]["amount"] == 6800
    assert rows[0]["quantity"] == 1


def test_ccgp_oss_attachment_links_are_discovered():
    from backend.services.research_sources import attachments

    source = {
        "url": "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/x.htm",
        "body_status": "fetched",
        "source_type": "tender_document",
        "body_text": (
            "附件信息：\n"
            "报价明细.doc [https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/a.doc]\n"
            "招标文件.docx [https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/b.docx]\n"
            "首页 [http://www.ccgp.gov.cn/]"
        ),
    }
    urls = attachments(source)
    assert urls == [
        "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/a.doc",
        "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/b.docx",
    ]


def test_markdown_and_download_attachment_links_are_discovered_without_punctuation():
    from backend.services.research_sources import attachments

    source = {
        "url": "https://procure.example.gov.cn/notice/view?id=7",
        "source_type": "award_document",
        "body_status": "fetched",
        "body_text": (
            "[报价明细](https://procure.example.gov.cn/file/download?id=9)\n"
            "[中标表](https://procure.example.gov.cn/files/award.xlsx?token=x)\n"
            "[首页](https://procure.example.gov.cn/)"
        ),
    }
    urls = attachments(source, model="未出现在行内的型号")
    assert urls == [
        "https://procure.example.gov.cn/file/download?id=9",
        "https://procure.example.gov.cn/files/award.xlsx?token=x",
    ]
