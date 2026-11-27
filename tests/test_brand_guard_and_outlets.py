# -*- coding: utf-8 -*-
"""品牌规范化的护栏，以及 4 个出口接上规范化（``output/brand-guard-fix/report.md``）。

上一单（``406ade2``）把正文直读那条路的品牌接上了 ``normalize_brand_value``，评估
（``output/brand-holes-audit/report.md``）随后查出**规范化自己会改坏合法品牌**：

* ``(卢)湘仪/湘鑫`` → ``卢)湘仪/湘鑫``（按字符集剥两端标点时把左括号剥掉了）；
* ``PHC Europe B.V. / PHCbi`` → ``''``（去空格 19 字，超过 16 字长度上限）。

这一组守两条线：

1. **护栏**：上面两个真实形态逐字不变；把护栏拿掉（本文件用
   :func:`_normalize_without_guards` 逐字复刻护栏之前那一版）两者立刻被改坏；
2. **4 个出口**（``procurement_tables.republished_award_rows`` / ``create_price_record``
   （一处盖住表格路与标签连写路）/ ``province_award_sources._lot_line`` /
   ``lead_alignment._row_pairs``）：脏值进去、干净值出来，且**回退出修复后确实产出脏值**
   （每个出口都用产品自己的那一层函数复算"未洗前的原值"）。

品牌判据一个字没动：``brand_name_matches`` / ``validate_price`` 的匹配逻辑照旧，
这里只钉"同一个价格行，修复前被拒、修复后被收下"。
"""
import unicodedata

import pytest

from backend.services import province_award_sources as pas
from backend.services.lead_alignment import _brand_before, _row_pairs
from backend.services.procurement_rules import (brand_from_source_line,
                                                normalize_brand_value,
                                                validate_price)
from backend.services.brand_model_rules import (_BRAND_EDGE_PUNCTUATION,
                                                         _BRAND_LEAD_WORDS,
                                                         _brand_cell_by_shape,
                                                         looks_like_brand_value,
                                                         row_cells)
from backend.services.procurement_tables import (RegexPatterns, align_cells,
                                                 label_line_rows,
                                                 parse_table_header,
                                                 price_table_rows,
                                                 republished_award_rows)

#: 四个真实形态：``(卢)湘仪/湘鑫``（真实语料里 16 行的那个值）、
#: ``PHC Europe B.V. / PHCbi``（真实候选里的斜杠并列写法）、
#: ``安捷伦（Agilent）``（不含斜杠、清完也不为空，但右括号会被剥掉、全角还会被折成半角）、
#: ``生元仪器``（四个汉字，只因为含 ``元`` 被 ``_NOT_A_BRAND_IN`` 的单字噪声清空）。
REAL_SLASH_WITH_BRACKETS = '(卢)湘仪/湘鑫'
REAL_LONG_ENGLISH_SLASH = 'PHC Europe B.V. / PHCbi'
REAL_FULLWIDTH_BRACKETS = '安捷伦（Agilent）'
REAL_NAME_WITH_A_NOISE_CHAR = '生元仪器'
REAL_SHAPES = (REAL_SLASH_WITH_BRACKETS, REAL_LONG_ENGLISH_SLASH,
               REAL_FULLWIDTH_BRACKETS, REAL_NAME_WITH_A_NOISE_CHAR)


def _normalize_without_guards(value) -> str:
    """护栏之前那一版的 ``normalize_brand_value``：逐行等价复刻，一个字不加。

    用它复算"护栏不存在时会怎样"：把产品代码里的两条护栏拿掉，本文件里
    ``test_the_two_real_shapes_are_broken_without_the_guards`` 立刻变红。
    """
    text = unicodedata.normalize('NFKC', str(value if value is not None else ''))
    text = text.strip().strip(_BRAND_EDGE_PUNCTUATION)
    while text and text[0] in _BRAND_LEAD_WORDS:
        text = text[1:].strip().strip(_BRAND_EDGE_PUNCTUATION)
    if not text or not looks_like_brand_value(text):
        return ''
    return text


# ---------------------------------------------------------------------------
# 一、护栏本身
# ---------------------------------------------------------------------------

def test_the_four_real_shapes_are_broken_without_the_guards():
    # 红确认：护栏之前这一版真的会把这四个真实形态改坏或清空，实际值如下。
    assert _normalize_without_guards(REAL_SLASH_WITH_BRACKETS) == '卢)湘仪/湘鑫'  # 丢了一个左括号
    assert _normalize_without_guards(REAL_LONG_ENGLISH_SLASH) == ''               # 品牌被清空
    assert _normalize_without_guards(REAL_FULLWIDTH_BRACKETS) == '安捷伦(Agilent'  # 丢右括号＋折半角
    assert _normalize_without_guards(REAL_NAME_WITH_A_NOISE_CHAR) == ''            # 四汉字被清空
    # 加护栏之后四个都逐字不变。
    for value in REAL_SHAPES:
        assert normalize_brand_value(value) == value


@pytest.mark.parametrize('value', ['喆图', '一恒', '湘仪', '奥豪斯', '湖南凯 达',
                                   '上海精宏实验设备有限公司', '贝克曼库尔特'])
def test_a_clean_brand_is_returned_unchanged(value):
    # 反例：本来干净的值一个字符都不许改。
    assert normalize_brand_value(value) == value


@pytest.mark.parametrize('value', ['', '   ', None, '（）', '。', '/', ' / '])
def test_empty_or_punctuation_only_does_not_raise_and_yields_empty(value):
    # 反例：空值 / None / 纯标点不崩，也不凭空造值（只有一个斜杠的不算品牌名）。
    assert normalize_brand_value(value) == ''


@pytest.mark.parametrize('value, expected', [
    ('喆图/DRETOP', '喆图/DRETOP'),      # 斜杠并列：逐字保留（护栏一）
    ('禾普/hepu', '禾普/hepu'),
    ('国宇仪器/CZGY', '国宇仪器/CZGY'),
    ('(卢)湘仪/湘鑫', '(卢)湘仪/湘鑫'),  # 括号也不许被剥
    ('| 博迅/BOXUN', '博迅/BOXUN'),      # 两端纯分隔符仍旧剥掉
])
def test_a_legal_slash_spelling_is_kept_verbatim(value, expected):
    assert normalize_brand_value(value) == expected


def test_the_existing_cleaning_rules_are_not_loosened():
    # 既有契约一条没放宽：残渣照洗，"明确不是字号"的照旧返回空。
    assert normalize_brand_value('为喆图。') == '喆图'
    assert normalize_brand_value('为喆图') == '喆图'
    assert normalize_brand_value('（喆图）') == '喆图'
    assert normalize_brand_value('为3M。') == ''      # 带数字：型号/金额，不是字号
    assert normalize_brand_value('详见附件') == ''    # 点名的占位词


def test_a_long_english_name_is_kept_instead_of_being_emptied():
    # 护栏二：只因为超过长度上限被清空的名字保留原值（它不是"这不是品牌"的证据）。
    long_name = 'Thermo Fisher Scientific'
    assert len(long_name.replace(' ', '')) > 16
    assert _normalize_without_guards(long_name) == ''
    assert normalize_brand_value(long_name) == long_name


def test_a_brand_whose_brackets_would_be_broken_keeps_its_original_spelling():
    """护栏一不是"含斜杠才管"：``安捷伦（Agilent）`` 不含斜杠、清完也不为空，但结果是坏的。

    尾部孤立的 ``）`` 在边缘标点集合里，被按字符集剥掉后只剩一个 ``(``；NFKC 还会把
    全角折成半角。括号配对一变就不采用清洗结果，原值（全角括号一起）逐字保留。
    """
    assert _normalize_without_guards(REAL_FULLWIDTH_BRACKETS) == '安捷伦(Agilent'
    assert normalize_brand_value(REAL_FULLWIDTH_BRACKETS) == REAL_FULLWIDTH_BRACKETS
    # 成对剥（``（喆图）``）是既有契约，不受这条安全阀影响。
    assert normalize_brand_value('（喆图）') == '喆图'
    assert normalize_brand_value('（安捷伦）') == '安捷伦'


def test_a_name_containing_a_single_char_noise_word_is_not_emptied():
    """护栏二不再拿 ``_NOT_A_BRAND_IN`` 的**单字**（``元``／``台``／``套``）当"不是字号"。

    ``生元仪器`` 四个汉字、没有标点也没有斜杠，只因为含 ``元`` 被清空——品牌为空，
    这台机器的价就再也对不上。真占位词（``详见附件``）与带数字的（``为3M。``）仍旧返回空。
    """
    assert _normalize_without_guards(REAL_NAME_WITH_A_NOISE_CHAR) == ''
    assert normalize_brand_value(REAL_NAME_WITH_A_NOISE_CHAR) == REAL_NAME_WITH_A_NOISE_CHAR
    assert normalize_brand_value('详见附件') == ''
    assert normalize_brand_value('为3M。') == ''


# ---------------------------------------------------------------------------
# 二、brand_from_source_line 会不会被同一副作用伤到
# ---------------------------------------------------------------------------

SOURCE_LINE_WITH_SLASH_BRAND = ('血型专用离心机 | (卢)湘仪/湘鑫 | XKA-2200 | 2台 | 12,400.00元')


def test_brand_from_source_line_is_hit_by_the_same_side_effect():
    """会伤到：三条路径取到的候选值是``(卢)湘仪/湘鑫``，护栏之前它会被剥成``卢)湘仪/湘鑫``。

    护栏写在 ``normalize_brand_value`` 内部，所以这一条路径一并被覆盖，不必单独改。
    """
    cells = row_cells(SOURCE_LINE_WITH_SLASH_BRAND)
    candidate = _brand_cell_by_shape(cells, 'XKA-2200', '离心机')
    assert candidate == REAL_SLASH_WITH_BRACKETS          # 切出来的候选值
    assert _normalize_without_guards(candidate) == '卢)湘仪/湘鑫'  # 护栏前这条路返回的就是这个
    assert brand_from_source_line(SOURCE_LINE_WITH_SLASH_BRAND, model='XKA-2200',
                                  category='离心机') == REAL_SLASH_WITH_BRACKETS


def test_the_long_english_slash_name_is_refused_before_normalize_on_this_route():
    """记录现状：这条路的三个入口都先用 ``looks_like_brand_value`` 挡一道，
    而 ``PHC Europe B.V. / PHCbi`` 因长度超限在**进入规范化之前**就被挡成空串。

    所以护栏二对它的意义不在这条路，而在没有前置形状判据的出口
    （``republished_award_rows`` 直接取"型号前一格"，见
    :func:`test_a_republished_row_with_a_residue_brand_is_cleaned`）。
    """
    line = ('台式离心机 | PHC Europe B.V. / PHCbi | ZDP-9082 | 2台 | 12,400.00元')
    assert looks_like_brand_value(REAL_LONG_ENGLISH_SLASH) is False
    assert brand_from_source_line(line, model='ZDP-9082', category='离心机') == ''


# ---------------------------------------------------------------------------
# 三、4 个出口的红确认与反例
# ---------------------------------------------------------------------------

# ---- 出口 1：republished_award_rows（报告的"二手成交记录"） ----

REPUBLISHED_WITH_RESIDUE = (
    '血型专用离心机 | 为喆图。 | ZDP-9082 | 某市疾病预防控制中心 | 某某医疗器械有限公司'
    ' | 2026-01-05 | 1.24万元 | 查看\n')
#: 真实语料原样（``tests/test_republished_award_records.py`` 的 REPUBLISHED_BODY 同一形态）。
REPUBLISHED_REAL_SLASH = (
    '血型专用离心机 | (卢)湘仪/湘鑫 | XKA - 2200 | 木垒哈萨克自治县人民医院'
    ' | 新疆新丝路惠康医疗科技有限公司 | 2026-05-19 | 1.75万元 | 查看\n')


def test_a_republished_row_with_a_residue_brand_is_cleaned():
    # 红确认：这一处修复前**没有**规范化，出口就是整格原值——先把原值复算出来。
    cells = [cell.strip() for cell in REPUBLISHED_WITH_RESIDUE.split('|')]
    assert cells[1] == '为喆图。'          # 型号前一格，修复前直接当 brand 报出去
    rows = republished_award_rows(REPUBLISHED_WITH_RESIDUE, 'ZDP-9082')
    assert [row['brand'] for row in rows] == ['喆图']


def test_a_republished_row_keeps_the_real_slash_brand_verbatim():
    # 真实形态：16 行都是这个值，修复后必须逐字保留（护栏一）。
    rows = republished_award_rows(REPUBLISHED_REAL_SLASH, 'XKA-2200')
    assert [row['brand'] for row in rows] == [REAL_SLASH_WITH_BRACKETS]


# ---- 出口 2 / 3：create_price_record（表格路 :585 与标签连写路 :860 同一处收口） ----

TABLE_WITH_RESIDUE = '\n'.join([
    '某某医院医疗设备采购中标（成交）结果公告',
    '货物名称 | 品牌 | 规格型号 | 数量 | 单价(元)',
    '电热恒温培养箱 | 为喆图。 | ZDP-9082 | 2台 | 12,400.00',
])
LABEL_LINE_WITH_RESIDUE = ('名称：电热恒温培养箱 品牌：为喆图。 规格型号：ZDP-9082'
                           ' 数量：2台 单价：12,400.00元')


def _table_cells_and_header():
    """把测试正文按 ``price_table_rows`` 自己的读法拆成"表头 + 数据格"。"""
    header_line, data_line = TABLE_WITH_RESIDUE.splitlines()[-2:]
    has_currency_context = bool(RegexPatterns.CURRENCY_CONTEXT.search(TABLE_WITH_RESIDUE))
    header = parse_table_header([cell.strip() for cell in header_line.split('|')],
                               has_currency_context)
    assert header is not None
    return [cell.strip() for cell in data_line.split('|')], header


def test_the_table_route_was_dirty_before_the_fix_and_is_clean_now():
    # 红确认：``create_price_record`` 收到的 ``brand`` 参数就是对齐层给的这一格原值。
    cells, header = _table_cells_and_header()
    aligned = align_cells(cells, header, 1)
    assert aligned['brands'][0] == '为喆图。'          # 修复前从这一处出去的值
    assert [row['brand'] for row in price_table_rows(TABLE_WITH_RESIDUE, 'ZDP-9082')] == ['喆图']


def test_the_label_line_route_was_dirty_before_the_fix_and_is_clean_now():
    # 红确认：标签路捕到的那一格原值（用产品自己的正则复算）。
    from backend.services.procurement_tables import _LABEL_FIELDS
    brand_pattern = dict(_LABEL_FIELDS)['brand']
    assert brand_pattern.search(LABEL_LINE_WITH_RESIDUE).group(1).strip() == '为喆图。'
    rows = label_line_rows(LABEL_LINE_WITH_RESIDUE, 'ZDP-9082')
    assert [row['brand'] for row in rows] == ['喆图']


@pytest.mark.parametrize('body, reader', [
    (TABLE_WITH_RESIDUE, lambda body: price_table_rows(body, 'ZDP-9082')),
    (LABEL_LINE_WITH_RESIDUE, lambda body: label_line_rows(body, 'ZDP-9082')),
])
def test_a_clean_brand_survives_both_table_routes(body, reader):
    # 反例：本来干净的值不被这两条路改动。
    cleaned = body.replace('为喆图。', '喆图')
    assert [row['brand'] for row in reader(cleaned)] == ['喆图']


# ---- 出口 4（政采云标项块）：province_award_sources._lot_line ----

LOT_BLOCK_HTML = ('<p>标项一</p><p>主要标的名称： 电热恒温培养箱</p><p>数量： 2.00</p>'
                  '<p>单价（元）： 12400.00</p><p>品牌:为喆图。</p>'
                  '<p>规格型号:ZDP-9082</p>')


def test_the_province_lot_block_was_dirty_before_the_fix_and_is_clean_now():
    # 红确认：这一格是"品牌: 到下一个标签之间"捕到的原值（用产品自己的正则复算）。
    assert pas.LOT_LABELS['brand'].search('品牌:为喆图。').group(1).strip() == '为喆图。'
    body = pas.clean_content(LOT_BLOCK_HTML, '')
    shaped = pas.shape_lot_blocks(body)
    assert shaped != body, '前提：这一份正文真的铺出了表'
    assert [row['brand'] for row in price_table_rows(shaped, 'ZDP-9082')] == ['喆图']


def test_a_lot_block_with_a_clean_brand_is_untouched():
    html = LOT_BLOCK_HTML.replace('为喆图。', '喆图')
    body = pas.clean_content(html, '')
    shaped = pas.shape_lot_blocks(body)
    assert [row['brand'] for row in price_table_rows(shaped, 'ZDP-9082')] == ['喆图']


# ---- 出口 5（对外拒绝理由文本）：lead_alignment._row_pairs ----

LEAD_ROW_BODY = ('序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价\n'
                 '1 | 电热恒温培养箱 | 为喆图。 | ZDP-9082 | 2 | 12400\n')
LEAD_ROW_SLASH_BODY = ('序号 | 名称 | 品牌 | 规格型号 | 数量 | 单价\n'
                       '1 | 血型专用离心机 | (卢)湘仪/湘鑫 | XKA-2200 | 2 | 12400\n')


def test_the_lead_rejection_reason_was_dirty_before_the_fix_and_is_clean_now():
    # 红确认：``_brand_before`` 就是"型号前面最近那一格"，修复前原样拼进对外理由文本。
    cells = [cell.strip() for cell in LEAD_ROW_BODY.splitlines()[1].split('|')]
    assert _brand_before(cells, 3, '培养箱') == '为喆图。'
    assert [pair[1] for pair in _row_pairs(LEAD_ROW_BODY, ('培养箱',))] == ['喆图']


def test_a_slash_brand_in_a_lead_row_is_kept_verbatim():
    assert [pair[1] for pair in _row_pairs(LEAD_ROW_SLASH_BODY, ('离心机',))] \
        == [REAL_SLASH_WITH_BRACKETS]


def test_a_lead_row_without_a_brand_still_yields_none():
    # 反例：没有品牌格时仍旧是 ``None``，理由文本的拼法不变。
    body = '序号 | 名称 | 规格型号 | 数量 | 单价\n1 | 电热恒温培养箱 | ZDP-9082 | 2 | 12400\n'
    assert [pair[1] for pair in _row_pairs(body, ('培养箱',))] == [None]


# ---------------------------------------------------------------------------
# 四、核价行为：脏值会让真机器的价被判品牌不符，修好之后能采信
# ---------------------------------------------------------------------------

PRICE_SOURCE = {'url': 'https://www.instrument.com.cn/netshow/SH100445/C227000.htm',
                'body_status': 'fetched', 'body_text': TABLE_WITH_RESIDUE,
                'source_type': 'tender_document'}
BASELINE = {'model': 'ZDP-9082', 'brand': '喆图'}


def test_the_price_verdict_changes_only_because_the_brand_was_cleaned():
    """一台真机器（喆图 ZDP-9082）的成交价，修复前被自己的残渣品牌判掉，修复后被收下。

    * 价格行那一侧的品牌来自表格路：修复前是 ``为喆图。``，修复后是 ``喆图``；
    * 候选那一侧的品牌本来就是 ``喆图``（用户要买的那台）。

    判据一个字没动：``brand_name_matches('喆图', '为喆图。')`` 修复前后都是 ``False``。
    """
    rows = price_table_rows(TABLE_WITH_RESIDUE, 'ZDP-9082')
    assert len(rows) == 1
    raw = {**rows[0], 'kind': 'award'}

    # 修复前：价格行的品牌就是表格格原值（对齐层可复算，见
    # test_the_table_route_was_dirty_before_the_fix_and_is_clean_now）。
    cells, header = _table_cells_and_header()
    dirty_brand = align_cells(cells, header, 1)['brands'][0]
    assert dirty_brand == '为喆图。'

    dirty_refusals: dict = {}
    dirty = validate_price({**raw, 'brand': dirty_brand}, PRICE_SOURCE, dict(BASELINE),
                           dirty_refusals)
    assert dirty is None
    assert dirty_refusals.get('reason_code') == 'brand_mismatch'
    # 拒绝台账里记的是**价格行**那一侧的品牌原文（``validate_price`` 的 ``other_brand``）。
    assert dirty_refusals.get('other_brand') == '为喆图。'

    refusals: dict = {}
    quote = validate_price(dict(raw), PRICE_SOURCE, dict(BASELINE), refusals)
    assert quote is not None, refusals
    assert refusals == {}
