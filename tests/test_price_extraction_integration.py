# -*- coding: utf-8 -*-
"""
集成测试：从搜索到附件解析到价格提取的完整流程
测试workbuddy修正后的三个关键问题是否解决
"""
import pytest


def test_ccgp_provider_filters_chinese_queries():
    """问题1：权威源召回 - CCGP provider应该只接受中文查询"""
    from backend.infrastructure.search_providers import CCGPSearchProvider
    from backend.domain.search import SearchQuery

    provider = CCGPSearchProvider()

    # 应该支持中文查询
    assert provider.supports(SearchQuery(text='知楚A2生物安全柜', purpose='price', limit=5))
    assert provider.supports(SearchQuery(text='生物安全柜 中标', purpose='tender', limit=5))

    # 不应该支持纯英文查询（ccgp主要是中文公告）
    assert not provider.supports(SearchQuery(text='biosafety cabinet', purpose='price', limit=5))
    assert not provider.supports(SearchQuery(text='centrifuge award', purpose='tender', limit=5))


def test_ccgp_query_cleaning_removes_operator_noise():
    """问题1：权威源召回 - 清洗查询关键词，去除搜索引擎操作符"""
    from backend.infrastructure.search_providers import CCGPSearchProvider

    # 正文检索（searchtype=2）能命中型号，型号因此成为真正会发出的关键词，
    # 而不是"只剩品牌型号就没得搜"。
    raw1 = '"知楚A2" 中标 成交 site:gov.cn'
    cleaned1 = CCGPSearchProvider.engine_query(raw1, brand='知楚', model='A2')
    assert 'site:' not in cleaned1
    assert '中标' not in cleaned1  # 关键词也被清理
    assert cleaned1 == 'A2'

    # 复杂查询：操作符与排除短语都不能进入关键词。
    # 2026-10-06 起「品牌 品类」组合是最精确的一档（实测「上海博迅 洁净工作台」
    # 命中的是含该品类的真实成交公告，而单品牌「上海博迅」只命中 10 条与品类无关的
    # 最新公告）。engine_query 取 ladder 的第一档，所以这里返回组合而不是单品牌。
    raw2 = '知楚 生物安全柜 招标公告 filetype:pdf -"赛默飞"'
    cleaned2 = CCGPSearchProvider.engine_query(raw2, brand='知楚')
    assert 'filetype:' not in cleaned2
    assert '-"' not in cleaned2
    assert cleaned2 == '知楚 生物安全柜'

    # 没有品牌和型号时，才回退到品类词。
    assert CCGPSearchProvider.engine_query(
        '生物安全柜 招标公告 filetype:pdf -"赛默飞"') == '生物安全柜'


def test_attachment_link_extraction_from_ccgp_oss():
    """问题2：附件识别 - 从政采网OSS格式中提取附件链接"""
    from backend.services.research_sources import attachments

    source = {
        'url': 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260921_12345.htm',
        'body_status': 'fetched',
        'source_type': 'tender_document',
        'body_text': (
            "附件信息：\n"
            "报价明细.doc [https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/detail.doc]\n"
            "招标文件.pdf [https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/tender.pdf]\n"
        )
    }

    urls = attachments(source)
    assert len(urls) == 2
    assert 'detail.doc' in urls[0]
    assert 'tender.pdf' in urls[1]


def test_attachment_with_model_filter():
    """问题2：附件识别 - 非tender_document类型需要型号或关键词匹配"""
    from backend.services.research_sources import attachments

    source = {
        'url': 'http://example.edu.cn/notice.html',
        'source_type': 'award_document',  # 非tender_document
        'body_text': (
            "第一项：知楚A2生物安全柜\n"
            "详见附件：http://example.edu.cn/files/detail_A2.pdf\n\n"
            "第二项：赛默飞离心机B3\n"
            "详见附件：http://example.edu.cn/files/detail_B3.pdf\n"
        )
    }

    # 指定型号A2，应该只提取包含A2的行的附件
    urls_a2 = attachments(source, model='A2')
    assert len(urls_a2) == 1
    assert 'detail_A2.pdf' in urls_a2[0]

    # 指定型号B3
    urls_b3 = attachments(source, model='B3')
    assert len(urls_b3) == 1
    assert 'detail_B3.pdf' in urls_b3[0]


def test_attachment_with_keyword_matching():
    """问题2：附件识别 - 即使没有型号匹配，关键词也能触发提取"""
    from backend.services.research_sources import attachments

    source = {
        'url': 'http://example.edu.cn/notice.html',
        'source_type': 'award_document',
        'body_text': (
            "中标明细表见附件：http://example.edu.cn/files/award_detail.xlsx\n"
            "其他文件：http://example.edu.cn/files/other.pdf\n"
        )
    }

    # 即使型号不匹配，"中标明细"关键词应该触发提取
    urls = attachments(source, model='不存在的型号')
    assert len(urls) >= 1
    assert any('award_detail.xlsx' in url for url in urls)


def test_pdf_table_extraction_preserves_pipe_format():
    """问题3：单价提取 - PDF表格应该解析为带|分隔符的格式"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    import fitz

    # 创建一个简单的表格PDF
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=400)

    # 画一个2x3的表格
    left, top, cell_w, cell_h = 40, 80, 150, 40
    for col in range(4):
        x = left + col * cell_w
        page.draw_line((x, top), (x, top + 3 * cell_h))
    for row in range(4):
        y = top + row * cell_h
        page.draw_line((left, y), (left + 3 * cell_w, y))

    # 填充内容
    page.insert_text((left + 5, top + 25), "型号", fontname="china-s", fontsize=10)
    page.insert_text((left + cell_w + 5, top + 25), "单价(元)", fontname="china-s", fontsize=10)
    page.insert_text((left + 2*cell_w + 5, top + 25), "数量", fontname="china-s", fontsize=10)
    page.insert_text((left + 5, top + cell_h + 25), "知楚A2", fontname="china-s", fontsize=10)
    page.insert_text((left + cell_w + 5, top + cell_h + 25), "74800", fontname="china-s", fontsize=10)
    page.insert_text((left + 2*cell_w + 5, top + cell_h + 25), "1", fontname="china-s", fontsize=10)

    payload = pdf.tobytes()
    pdf.close()

    # 解析PDF
    body = PublicWebSearchProvider.decode_body(payload, 'application/pdf')

    # 应该包含带|分隔符的行
    assert '型号 | 单价(元) | 数量' in body
    assert '知楚A2 | 74800 | 1' in body


def test_price_extraction_from_pdf_attachment():
    """问题3：单价提取 - 从PDF附件表格中提取单价"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    from backend.services.procurement_tables import price_table_rows
    import fitz

    # 创建模拟的中标明细PDF
    pdf = fitz.open()
    # Five 180-point columns plus 40-point margins must fit on the page.
    page = pdf.new_page(width=980, height=600)

    # 添加标题
    page.insert_text((40, 50), "中标公告 - 生物安全柜采购项目", fontname="china-s", fontsize=12)

    # 画表格
    left, top, cell_w, cell_h = 40, 80, 180, 40
    headers = ['货物名称', '型号', '品牌', '单价(元)', '数量']
    assert left + len(headers) * cell_w <= page.rect.width - 40
    data = [
        ['生物安全柜', '知楚A2', '知楚', '74,800', '1'],
        ['生物安全柜', '赛默飞B3', '赛默飞', '105,000', '2']
    ]

    for col in range(len(headers) + 1):
        x = left + col * cell_w
        page.draw_line((x, top), (x, top + (len(data) + 1) * cell_h))
    for row in range(len(data) + 2):
        y = top + row * cell_h
        page.draw_line((left, y), (left + len(headers) * cell_w, y))

    for col, header in enumerate(headers):
        page.insert_text((left + col * cell_w + 5, top + 25), header, fontname="china-s", fontsize=9)
    for row_idx, row_data in enumerate(data):
        for col, value in enumerate(row_data):
            page.insert_text(
                (left + col * cell_w + 5, top + (row_idx + 1) * cell_h + 25),
                value, fontname="china-s", fontsize=9
            )

    payload = pdf.tobytes()
    pdf.close()

    # 解析PDF
    body = PublicWebSearchProvider.decode_body(payload, 'application/pdf')

    # 提取价格
    prices = price_table_rows(body, '知楚A2')

    assert len(prices) == 1
    assert prices[0]['amount'] == 74800.0
    assert prices[0]['amount_unit'] == '元'
    assert prices[0]['quantity'] == 1.0
    assert prices[0]['brand'] == '知楚'
    assert prices[0]['amount_scope'] == 'unit'


def test_fujian_ccgp_inline_table_format():
    """问题3：单价提取 - 福建政采网内嵌表格格式"""
    from backend.services.procurement_tables import price_table_rows

    body = "\n".join([
        "四、主要标的信息",
        "合同包1:",
        "货物类（知楚生物科技有限公司）",
        "品目号 | 品目名称 | 采购标的 | 品牌 | 规格型号 | 数量（单位） | 单价(元) | 总价(元)",
        "1-1 | 生物安全柜 | 生物安全柜 | 知楚 | A2 | 1.00(台) | 74,800.00 | 74,800.00",
        "1-2 | 生物安全柜 | 生物安全柜 | 知楚 | B2 | 2.00(台) | 85,000.00 | 170,000.00",
    ])

    prices_a2 = price_table_rows(body, 'A2')
    assert len(prices_a2) == 1
    assert prices_a2[0]['amount'] == 74800.0
    assert prices_a2[0]['quantity'] == 1.0
    assert prices_a2[0]['brand'] == '知楚'

    prices_b2 = price_table_rows(body, 'B2')
    assert len(prices_b2) == 1
    assert prices_b2[0]['amount'] == 85000.0
    assert prices_b2[0]['quantity'] == 2.0


def test_multi_value_alignment_semicolon_separated():
    """问题3：单价提取 - 多值对齐（分号分隔）"""
    from backend.services.procurement_tables import price_table_rows

    body = "\n".join([
        "货物名称 | 规格型号 | 货物品牌 | 单价(万元) | 货物数量",
        "生物安全柜 | A2;B2 | 知楚;知楚 | 7.48;8.5 | 1;2",
    ])

    # 提取A2型号
    prices_a2 = price_table_rows(body, 'A2')
    assert len(prices_a2) == 1
    assert prices_a2[0]['model'] == 'A2'
    assert prices_a2[0]['brand'] == '知楚'
    assert prices_a2[0]['amount'] == 7.48
    assert prices_a2[0]['amount_unit'] == '万元'
    assert prices_a2[0]['quantity'] == 1.0

    # 提取B2型号
    prices_b2 = price_table_rows(body, 'B2')
    assert len(prices_b2) == 1
    assert prices_b2[0]['model'] == 'B2'
    assert prices_b2[0]['amount'] == 8.5
    assert prices_b2[0]['quantity'] == 2.0


def test_budget_not_confused_with_award_price():
    """重要：预算不应该被识别为成交价"""
    from backend.services.procurement_tables import price_table_rows

    # 表格里的价格是成交价，应该能提取
    body_with_table = "\n".join([
        "项目预算：150,000元",
        "",
        "型号 | 单价(元) | 数量",
        "知楚A2 | 74,800 | 1",
    ])

    prices = price_table_rows(body_with_table, '知楚A2')
    assert len(prices) == 1
    assert prices[0]['amount'] == 74800.0  # 应该是表格里的成交价，不是预算


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
