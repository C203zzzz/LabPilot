"""跨模板回归：三种公告版式在比价链上的真实行为。

这一轮之前，比价链只在一个型号、一种公告版式（逐字段换行）上验证过。三个
fixture 都是**真实公告的逐字摘录**，分别代表另外三种版式：

* ``ccgp-table-semicolon-parallel.txt``：CCGP「主要标的信息」``<table>``，型号与
  金额在同一格里用「；」并列 27 项。
* ``ccgp-whole-package-parallel.txt``：同一张表的「一、二、三…」整包并列形态，
  全行只有一个金额（整包总价），不是其中任一型号的单价。
* ``hospital-sentence-parallel.txt``：成交公告用空格与「。」并列 5 台设备，
  ``split_cells`` 只认「；」，切不开。
* ``zip-price-detail.txt``：``报价明细附件.zip`` 里两个 PDF 成员被解码后的正文。

每个测试都标注它钉的是**已验证的正确行为**还是**当前缺口**。
"""
from pathlib import Path

from backend.services.procurement_rules import validate_price
from backend.services.procurement_tables import parse_amount, price_table_rows
from backend.services.research_sources import has_amount_evidence, price_regions

FIXTURES = Path(__file__).parent / 'fixtures' / 'template-regression'


def body(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def source(text, url):
    return {'url': url, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': text, 'publication_date': '2026-09-23'}


#: 附件本身是「分项报价表」，通篇不写「中标」；措辞来自发布它的那条公告。
#: 真实链路里这段以 ``publication_context`` 附在附件来源上（见
#: ``procurement_research.fetch_attachments`` 与 ``source_authority.linked_publication``）。
NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260923_27391944.htm'
NOTICE_TEXT = ('彩色多普勒超声诊断系统及便携式彩色多普勒超声诊断仪项目结果公告\n'
               '三、采购结果\n'
               '供应商名称 | 供应商地址 | 中标（成交）金额\n'
               '中盈华诺（广东）科技有限公司 | 广州市越秀区解放北路960号1207室 | 3,336,000.00元\n')


def attachment_source(text, url):
    return {**source(text, url), 'parent_url': NOTICE_URL,
            'publication_context': {'url': NOTICE_URL, 'body_text': NOTICE_TEXT,
                                    'linked_url': url}}



def package_row(text):
    return next(line for line in text.splitlines() if line.startswith('包别1 | 药理评价系统'))


#: 扫描件型附件：``设计规范`` 那份「分项报价表」，两页都是图片、没有文字层，
#: 下面的正文是产品自己的本地解析链（Docling + RapidOCR）读出来的结果。
SCANNED_QUOTE_NOTICE_URL = (
    'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202510/t20251027_25581686.htm')
SCANNED_QUOTE_NOTICE_TEXT = (
    '雄安宣武医院运行建设相关的补充医疗设备项目第十一包（手术导航系统、超声骨刀等）中标公告\n'
    '总中标金额 | ￥564.850000 万元（人民币）\n')


def scanned_quote_source(text, url):
    return {**source(text, url), 'parent_url': SCANNED_QUOTE_NOTICE_URL,
            'publication_context': {'url': SCANNED_QUOTE_NOTICE_URL,
                                    'body_text': SCANNED_QUOTE_NOTICE_TEXT,
                                    'linked_url': url}}


# --------------------------------------------------------------------------- 表格型
def test_table_cells_joined_by_pipe_are_read_and_the_right_one_is_priced():
    """已验证：``<table>`` 转成 ``a | b | c`` 行后能取价，且分号并列逐项对齐。

    这一行里型号格与金额格各用「；」并列 27 项，``DW-86L578S`` 是第 3 项，
    对应第 3 个金额 150000、第 3 个数量 2 台、第 3 个品牌 海尔。错位一位就是
    把别的设备的价挂到这个型号上。
    """
    text = body('ccgp-table-semicolon-parallel.txt')
    rows = price_table_rows(text, 'DW-86L578S')

    assert len(rows) == 1, '同一行只该产出目标型号那一条'
    row = rows[0]
    assert row['amount'] == 150000
    assert row['quantity'] == 2
    assert row['brand'] == '海尔'
    assert 'DW-86L578S' in row['excerpt']

    checked = validate_price({**row, 'kind': 'award'},
                             source(text, 'http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/'
                                          't20260812_27118698.htm'),
                             {'model': 'DW-86L578S', 'brand': '海尔', 'quantity': 2,
                              'currency': 'CNY'})
    assert checked is not None
    assert checked['unit_price'] == 150000


def test_whole_package_row_never_becomes_a_unit_price():
    """已验证：整包并列行的金额既进不了表格通道，也过不了验收口。

    修复前，这一行的 1,239,000 元（整包总价）被当成 ``DW-86L578S`` 的单台单价
    写进 ``prices[]``（见 设计规范）。现在表格通道由 ``lists_sibling_models``
    整行跳过；即使模型车道照原样报出这一行，验收口也不承认它是单价——
    该行的金额没有可绑定的币种写法。
    """
    text = body('ccgp-whole-package-parallel.txt')
    assert price_table_rows(text, 'DW-86L578S') == []

    refusals = {}
    row = {'kind': 'award', 'model': 'DW-86L578S', 'brand': '', 'amount': 1239000,
           'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'currency': 'CNY',
           'excerpt': package_row(text)}
    checked = validate_price(row, source(text, 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/'
                                               't20251209_25889616.htm'),
                             {'model': 'DW-86L578S', 'brand': '海尔', 'quantity': 1,
                              'currency': 'CNY'}, refusals)
    assert checked is None, '整包总价不得变成单价'
    assert refusals['reason_code'] in {'unparsed_amount', 'amount_not_unit_price',
                                       'total_not_unit_price'}


def test_sentence_parallel_row_is_skipped_rather_than_misattributed():
    """已验证：用「。」与空格并列的行一条价格都不产出——但也绝不张冠李戴。

    ``split_cells`` 只按「；」切，这一行切不开，于是 ``lists_sibling_models``
    判定型号格里并列了别的型号并整行跳过。宁可 0 条，不能把 35000 元
    （``BSC1600-IIA2``）挂到 ``BSC1300-IIA2`` 头上。
    """
    text = body('hospital-sentence-parallel.txt')
    for model in ('BSC1300-IIA2', 'BSC1600-IIA2', 'BSC1000-IIA2'):
        assert price_table_rows(text, model) == []


# --------------------------------------------------------------------------- 附件型
def test_zip_attachment_body_keeps_the_member_name_it_came_from():
    """已验证：``报价明细附件.zip`` 能解码，包内文件名留在正文里供引用。

    没有这一行，引用就无法说明这条价格出自压缩包里哪一个文件（见 设计规范）。
    """
    text = body('zip-price-detail.txt')
    assert text.count('【附件包内文件】') == 2, '包内两个 PDF 都应留下来源'
    assert 'EPIQ 7' in text and '1,668,000' in text
    assert 'X1' in text and '195,800' in text


def test_attachment_line_item_table_with_unit_in_the_header_is_priced():
    """已验证（真跑读到的附件）：列头自带单位时，附件型照样能取价。

    这一段来自真实链路在 attachments 阶段读进来的一张明细表附件
    （``download.ccgp.gov.cn/oss/download?uuid=9f2cde47…``，报告里 26 个来源之一）。
    它的表头是 ``… | 单价（元） | 数量 | 单位 | 总价（元）``：**列头自己带了单位**，
    于是哪怕全文没有「金额单位：元」这类声明，``is_price_heading`` 也直接认这一列，
    表格通道逐行取价，验收口全部通过。
    """
    text = body('attachment-line-item-table.txt')
    rows = price_table_rows(text, 'BSC- 1304IIA2')

    assert len(rows) == 1
    row = rows[0]
    assert row['amount'] == 45000
    assert row['quantity'] == 8

    checked = validate_price({**row, 'kind': 'award'},
                             attachment_source(text, 'http://download.ccgp.gov.cn/oss/download'
                                                     '?uuid=9f2cde47-a699-4f2e-a141-e752d5'),
                             {'model': 'BSC- 1304IIA2', 'quantity': 8, 'currency': 'CNY'})
    assert checked is not None
    assert checked['unit_price'] == 45000


def test_scanned_itemized_quote_reads_the_unit_price_today():
    """已验证：纯图片扫描件上的单价 49500 现在能走完整条验收口。

    正文来自产品的本地解析链读 ``设计规范`` 那份两页扫描件的结果。修好之前
    ``price_table_rows`` 对它返回 **0 行**，因为型号格写的是 ``BSC-1604 IIA2-e``
    ——空格是**型号内部**的写法（``contains_model`` 一直认这种写法），而
    ``_MODEL_SHAPED`` 把它切成 ``BSC-1604`` 与 ``IIA2`` 两个标记，
    ``lists_sibling_models`` 于是把这一格当成「并列着别的型号」，整行丢掉。
    品牌列写 ``苏州 安泰``，与比较基准 ``苏净安泰`` 也在同一张别名表里归一。
    """
    text = body('scanned-itemized-quote-ocr.txt')
    rows = price_table_rows(text, 'BSC-1604IIA2-e')

    assert len(rows) == 1
    row = rows[0]
    assert row['amount'] == 49500
    assert row['amount_unit'] == '元'
    assert row['amount_scope'] == 'unit'
    assert row['brand'] == '苏州 安泰'
    # 相邻的单人型（BSC-1304，47000）不得借到这一行上。
    assert 'BSC-1304' not in row['excerpt'] and '47000' not in row['excerpt']

    checked = validate_price({**row, 'kind': 'award'},
                             scanned_quote_source(text, 'http://ccgp-hebei.gov.cn/'
                                                        'BidWinAnncFiles/0063507b-3b43-4808-'
                                                        'ac3a-2f45298ceac5.pdf'),
                             {'model': 'BSC-1604IIA2-e', 'brand': '苏净安泰',
                              'quantity': 1, 'currency': 'CNY'})
    assert checked is not None, '扫描件上的单台单价 49500 必须进得了验收口'
    assert checked['unit_price'] == 49500


def test_zip_line_item_amounts_are_not_read_as_prices_today():
    """当前缺口（不是期望行为）：这张明细表的列头是裸「单价」，于是读不出来。

    差别只在这一处：附件里的「分项报价表」写成 ``… | 单价 | 数量 | 总价``，列头不带
    单位，包内文本的币种声明又是「货币及单位：人民币/元」——``has_currency_context``
    只认「金额单位：元」／「元(人民币)」，认不出这一种，于是「单价」列头不被当作价格列，
    整表没有表头可认。两条通道都断在这一点上：

    * 表格通道：``price_table_rows`` 返回 0 行；
    * 模型通道：窗口里没有「金额 + 单位」同行的写法，``has_amount_evidence`` 为假，
      抽取模型一次都不被调用。

    修好之后这两条断言要改成 1 行与 True。
    """
    text = body('zip-price-detail.txt')
    assert price_table_rows(text, 'EPIQ 7') == []

    regions = price_regions(text, 'EPIQ 7')
    assert regions, '型号窗口本身找得到'
    assert not any(has_amount_evidence(region['text']) for region in regions)

    refusals = {}
    checked = validate_price({'kind': 'award', 'model': 'EPIQ 7', 'brand': '飞利浦',
                              'amount': 1668000, 'amount_unit': '元', 'amount_scope': 'unit',
                              'quantity': 2, 'currency': 'CNY', 'date': '2026-09-23',
                              'excerpt': next(line for line in text.splitlines()
                                              if line.startswith('1-1 | 1 |'))},
                             attachment_source(text, 'https://gdgpo.czt.gd.gov.cn/gpx-bid-file/x.zip'),
                             {'model': 'EPIQ 7', 'brand': '飞利浦', 'quantity': 2,
                              'currency': 'CNY'}, refusals)
    assert checked is None
    assert refusals['reason_code'] == 'unparsed_amount', \
        '附件正文里的金额是裸数字，绑不上币种'


def test_cell_that_repeats_its_column_label_is_not_read_today():
    """当前缺口（不是期望行为）：单元格自己重复列名时金额解析不出来。

    真实公告（中南民族大学设备购置，``caizhao.scuec.edu.cn/zb/jghw/15169.chtml``）
    的数据格写成 ``核心产品单价：158000元``：列名、冒号、数字、单位挤在一格里。
    ``parse_amount`` 要求整格是数字（可带千分位/小数，或数字带单位），于是整条
    记录被丢弃。修好之后这里应解析出 158000。
    """
    assert parse_amount('核心产品单价：158000元', '元') is None
    assert parse_amount('158000元', '元') == 158000
