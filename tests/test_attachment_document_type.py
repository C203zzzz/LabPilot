# -*- coding: utf-8 -*-
"""抓取之后的正文判据：附件自己的正文说它是什么文书，值不值得当证据。

为什么需要这一层（真名样本的出处见 ``设计规范-附件关键文件判据.md``）：41 条真实
附件里含采购需求／配置类关键词的只有 10 条，其中 3 条是假阳性；而附件名靠不住——
最有价值的那几份名字是 UUID（``4baf8799-f6bb-464c-af58-8fc395965013.pdf`` 是含
配置的安徽招标文件、``a38f7c6d-667b-40c6-973f-868f839cf1a0.pdf`` 是带真单价的
中标标的表），或者拼音首字母（``xqyesmh.pdf`` 就是「中小企业声明函」）。
``attachment_rank`` 只能在抓取前看 label，这些名字它一个都匹配不上。

所以这一层按**文书自己的开头**判。两个窗口故意不对称，理由在
``research_sources._ATTACHMENT_TITLE_LINES``：噪声判据只用最前两行（合肥大学那份
585 字的《主要中标标的承诺函》第三行才出现「承诺函」，而它的表体里带着一条真实成交
单价 1239000.00 元），招标文件认定用八行（封面把类型名拆成竖排单字）。

夹具全部是真实附件正文的逐字切片（``tests/fixtures/attachment-evidence/``），
只有 ``help-platform-manual-shape.txt`` 是形状夹具：真实那份是海口那条公告的
``海口市公共资源交易中心新版网站常用功能操作说明.docx``（label 实测为
``'新版操作说明 '``），它在本机 41 条附件的实测里 ``body_status=failed``、0 字。
"""
from pathlib import Path

from backend.services.research_sources import (ATTACHMENT_FORM, ATTACHMENT_PLAIN,
                                               attachment_document_type, attachment_rank,
                                               is_evidence_attachment,
                                               key_attachment_documents)

FIXTURES = Path(__file__).parent / 'fixtures' / 'attachment-evidence'


def document(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


#: 类型 → 夹具。左侧是判据应该给出的答案，右侧是真实样本的来处。
KEPT = {
    'tender-anhui-demo-cover.txt': 'tender',
    'tender-haikou-cover.txt': 'tender',
    'tender-shaoxing-cover.txt': 'tender',
    'award-table-nxu.txt': 'award_table',
    'award-table-anhui-commitment.txt': 'award_table',
    'award-table-neimenggu-doubled.txt': 'award_table',
    'award-reason-shanghai.txt': 'award_reason',
}
DROPPED = {
    'form-imust-declaration.txt': 'form',
    'form-jilin-declaration.txt': 'form',
    'record-opening-wmu.txt': 'record',
    'record-opening-zcy-footer.txt': 'record',
    'record-qualification-zcy-footer.txt': 'record',
    'record-expert-draw-wmu.txt': 'record',
    'record-score-sheet-haikou.txt': 'record',
    'roster-hunan-companies.txt': 'roster',
    'roster-gov-contact-list.txt': 'roster',
    # 形状夹具：真实那份（``海口市公共资源交易中心新版网站常用功能操作说明.docx``）在本机
    # 41 条附件的实测里 ``body_status=failed``、0 字，拿不到正文，所以这份是手写的同类形状。
    'help-platform-manual-shape.txt': 'help',
}


def test_real_tender_documents_are_the_ones_that_are_kept():
    """六份真实招标文件（29712–55722 字）是附件里唯一的主要价值来源。"""
    for name, kind in KEPT.items():
        body = document(name)
        assert attachment_document_type(body) == kind, name
        assert is_evidence_attachment(body) is True, name


def test_real_forms_and_scoring_records_are_dropped():
    """声明函／承诺函／评标事务文书／名录不是采购需求，也不是成交价。"""
    for name, kind in DROPPED.items():
        body = document(name)
        assert attachment_document_type(body) == kind, name
        assert is_evidence_attachment(body) is False, name


def test_a_name_that_says_nothing_is_kept_but_graded_plain():
    """安徽那份含配置的招标文件名是 UUID：抓取前只能 PLAIN，抓取后靠正文留住。

    判据的一半一半分工就在这里：``attachment_rank`` 看名字（UUID → 一般），
    ``is_evidence_attachment`` 看正文（``公开招标文件示范文本`` → 关键）。名字里带
    「示范文本」**不是**降级理由——那份 47590 字的文件装的是合肥大学该项目的真实需求。
    """
    body = document('tender-anhui-demo-cover.txt')
    assert '公开招标文件示范文本' in body
    assert attachment_rank('4baf8799-f6bb-464c-af58-8fc395965013.pdf') == ATTACHMENT_PLAIN
    assert attachment_rank('4baf8799-f6bb-464c-af58-8fc395965013.pdf') != ATTACHMENT_FORM
    assert is_evidence_attachment(body) is True


def test_a_commitment_letter_that_carries_a_real_price_is_kept():
    """合肥大学那份 585 字附件：正文第 3 行起写「承诺函」，但表体里有一条真单价。

    窗口放宽到三行就会把这份真价格表当格式函件丢掉，所以噪声判据只认最前两行。
    """
    body = document('award-table-anhui-commitment.txt')
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    assert '承诺函' not in '\n'.join(lines[:2])
    assert '承诺函' in body
    assert '1239000.00' in body and '药理' in body
    assert attachment_document_type(body) == 'award_table'
    assert is_evidence_attachment(body) is True


def test_a_declaration_that_mentions_the_tender_document_is_not_one():
    """吉林那份中小企业声明函第 6 行写着「采购文件中明确的所属行业」。

    按全文找「采购文件」会把它判成招标文件、白读一次；按封面两行判就是声明函。
    这一条是窗口设计的直接反例。
    """
    body = document('form-jilin-declaration.txt')
    assert '采购文件' in body
    assert '中小企业声明函' in '\n'.join(
        [line.strip() for line in body.splitlines() if line.strip()][:2])
    assert attachment_document_type(body) == 'form'
    assert is_evidence_attachment(body) is False


def test_a_record_sheet_whose_title_is_printed_at_the_bottom_is_still_a_record():
    """政采云把《开标记录表》《资格审查记录表》的类型名印在页脚，开头只有字段名。"""
    for name in ('record-opening-zcy-footer.txt', 'record-qualification-zcy-footer.txt'):
        body = document(name)
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        assert '记录表' not in '\n'.join(lines[:2]), name
        assert '记录表' in body[-300:], name
        assert attachment_document_type(body) == 'record', name
        assert is_evidence_attachment(body) is False, name


def test_a_document_that_cannot_be_recognised_is_kept():
    """认不出来按「保留」处理，不按噪声丢：文档类型是开放集合，判据宁缺勿滥。"""
    assert attachment_document_type('项目编号：X-1\n采购人：某单位\n一些没有类型的正文') == 'unknown'
    assert is_evidence_attachment('项目编号：X-1\n采购人：某单位\n一些没有类型的正文') is True
    assert attachment_document_type('') == 'unknown'
    assert attachment_document_type(None) == 'unknown'


def test_key_attachment_documents_keeps_order_and_drops_the_noise():
    """接线的纯函数：给一批已抓取的 documents，返回其中的关键文件，顺序不变。"""
    documents = [
        {'url': 'https://x/1', 'body_text': document('form-jilin-declaration.txt')},
        {'url': 'https://x/2', 'body_text': document('tender-haikou-cover.txt')},
        {'url': 'https://x/3', 'body_text': document('record-opening-wmu.txt')},
        {'url': 'https://x/4', 'body_text': document('award-table-anhui-commitment.txt')},
        {'url': 'https://x/5', 'body_text': ''},
        {'url': 'https://x/6'},
    ]
    kept = key_attachment_documents(documents)
    assert [item['url'] for item in kept] == ['https://x/2', 'https://x/4']
    # 同一个 dict 对象，不是拷贝：调用方拿回去还要用它的 url / parent_url。
    assert kept[0] is documents[1]
    assert key_attachment_documents([]) == []


def test_a_platform_manual_stays_evidence_on_the_publishers_own_page():
    """操作说明／使用手册在公告附件里是噪声，在厂家页面上是参数的第一手证据。

    这条历史判据还在 ``tests/test_attachment_selection.py``
    （``test_a_brand_manual_stays_readable_on_an_untrusted_page``），所以同一份
    「说明书」类文书在两处上下文里必须有两个答案。
    """
    body = document('help-platform-manual-shape.txt')
    assert is_evidence_attachment(body) is False
    assert is_evidence_attachment(body, 'tender_document') is False
    assert is_evidence_attachment(body, 'manufacturer_website') is True
    assert is_evidence_attachment(body, 'manufacturer_download') is True
    documents = [{'url': 'https://vendor/q20-manual', 'body_text': body}]
    assert key_attachment_documents(documents) == []
    assert key_attachment_documents(documents, 'manufacturer_website') == documents


def test_the_two_windows_are_different_on_purpose():
    """抬头两行 vs 八行：一份招标文件的目录里会提到每一种文书。"""
    cover = document('tender-shaoxing-cover.txt')
    lines = [line.strip() for line in cover.splitlines() if line.strip()]
    # 竖排封面：前两行里没有「招标文件」连成词，第四到第八行才有「招标编号」。
    assert '招标文件' not in '\n'.join(lines[:2])
    assert '招标编号' in '\n'.join(lines[:8])
    assert attachment_document_type(cover) == 'tender'
