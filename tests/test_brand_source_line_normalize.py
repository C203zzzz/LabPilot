"""``brand_from_source_line`` 接上品牌规范化：解析残渣不再流进品牌判据。

为什么要有这一组（``output/brand-residual/report.md`` §3 的"仍存在的同类洞"、
``output/brand-normalize-fix/report.md``）：正文直读那条路（``text_scan_candidates``
→ ``brand_from_source_line``）切出来的品牌**没有过** ``normalize_brand_value``。
行里就地写着 ``品牌：为喆图。`` 时，切出来的是 ``为喆图。``——带着引导字与句号也算
"有品牌"，于是拿它去和价格行的 ``喆图`` 比：``brand_name_matches('喆图','为喆图。')``
判 False，**真查到的喆图成交价会被判"品牌不符"拒掉**（``validate_price`` 的
``BRAND_MISMATCH``）。

这一组守两条线，方向相反：

* 残渣必须被洗掉（``为喆图。`` → ``喆图``）；把规范化回退掉，本文件第一条用例就红；
* 品牌判据一个字没放宽——``brand_name_matches`` / ``validate_price`` 的匹配逻辑没动，
  这里只是钉住"同一个价格行，修复前被拒、修复后被收下"。
"""
import pytest

from backend.services.procurement_rules import (brand_from_source_line, brand_identity,
                                                brand_name_matches, validate_price)
from backend.services.brand_model_rules import (BRAND_NOISE, _brand_cell_by_shape,
                                                         _brand_cell_from_header,
                                                         _brand_from_inline_label, row_cells)

#: 真实形态：表格行，品牌那一格就地写成 ``品牌：为喆图。``（引导字与句号都在值里）。
#: 同一句话的形态见 ``output/brand-residual/report.md`` 证据 4 的落盘正文
#: ``…具体型号是ZDP-9082，品牌为喆图。``——那句没有分格符，读不出值；带分格符之后
#: 才是这条路真正会遇到的残渣形态。
RESIDUE_LINE = '台式常温高速离心机 | 品牌：为喆图。 | ZDP-9082 | 2台 | 12,400.00元'
#: 真实形态：表头列名与数据格写在**同一行**（``sample-d`` 那篇公告的形状），品牌格是残渣。
HEADERED_RESIDUE_LINE = ('四、主要标的信息 包号：1 名称 品牌 规格型号 数量 单价 '
                         '| 小型台式离心机3 | 为喆图。 | TGL-20MC | 19台 | 14,500.00元')
#: 反例素材：``sample-d`` 那一轮真实的原文行，品牌本来干净。
CLEAN_LINE = '台式常温高速离心机 | 湘智 | G21000 | 2台 | 12,400.00元'


def _before_the_fix(line, model='', category=''):
    """修复前那一版的 ``brand_from_source_line``：逐行等价复刻，三条路径都裸返回。

    用同一批子函数与同一张噪声表，所以"回退版吐残渣"这句话可以在这里复算，
    不靠记忆：把产品代码里的 ``normalize_brand_value`` 两处调用拿掉，本文件里
    ``test_the_residue_is_gone_and_the_unhardened_version_still_shows_it`` 的
    第二、三条断言立刻变红。
    """
    cells = row_cells(line)
    if len(cells) < 3:
        return ''
    if any(noise in line for noise in BRAND_NOISE):
        return ''
    for candidate in (_brand_cell_from_header(cells), _brand_from_inline_label(line)):
        if candidate:
            return candidate
    return _brand_cell_by_shape(cells, model, category)


def test_the_residue_is_gone_and_the_unhardened_version_still_shows_it():
    # 红确认：同一条输入，回退版给 ``为喆图。``，新版给 ``喆图``；两值必须不同。
    old = _before_the_fix(RESIDUE_LINE, model='ZDP-9082', category='离心机')
    new = brand_from_source_line(RESIDUE_LINE, model='ZDP-9082', category='离心机')
    assert old == '为喆图。'          # 回退后的取值就是这个残渣
    assert new == '喆图'              # 修复后的取值
    assert old != new


def test_the_header_column_route_is_normalised_too():
    # 三条路径同一套规范化：表头列名路径切出来的残渣同样要洗掉。
    assert _before_the_fix(HEADERED_RESIDUE_LINE, model='TGL-20MC', category='离心机') == '为喆图。'
    assert brand_from_source_line(HEADERED_RESIDUE_LINE, model='TGL-20MC',
                                  category='离心机') == '喆图'


def test_a_trailing_full_stop_without_a_lead_word_is_cleaned():
    line = '台式高速离心机 | 品牌：喆图。 | H1650 | 2台 | 8,000元'
    assert _before_the_fix(line, model='H1650', category='离心机') == '喆图。'
    assert brand_from_source_line(line, model='H1650', category='离心机') == '喆图'


def test_a_clean_brand_is_returned_unchanged():
    # 反例：本来干净的值一个字符都不许改（三条路径各验一条）。
    assert brand_from_source_line(CLEAN_LINE, model='G21000', category='离心机') == '湘智'
    assert _before_the_fix(CLEAN_LINE, model='G21000', category='离心机') == '湘智'
    headered = ('四、主要标的信息 包号：1 名称 品牌 规格型号 数量 单价 '
                '| 小型台式离心机3 | 湘智 | TGL-20MC | 19台 | 14,500.00元')
    assert brand_from_source_line(headered, model='TGL-20MC', category='离心机') == '湘智'
    labelled = '台式高速离心机 | 品牌：飞鸽 | H1650 | 2台 | 8,000元'
    assert brand_from_source_line(labelled, model='H1650', category='离心机') == '飞鸽'
    # 别名归一仍然只归 ``brand_identity`` 管，这一层不改写原文写法。
    aliased = '台式高速离心机 | 湖南湘仪 | H1650 | 2台 | 8,000.00元'
    assert brand_from_source_line(aliased, model='H1650', category='离心机') == '湖南湘仪'
    assert brand_identity('湖南湘仪') == '湘仪'


def test_an_empty_line_or_none_does_not_raise():
    # 反例：空 / None 不崩，也不凭空造值。
    assert brand_from_source_line('') == ''
    assert brand_from_source_line(None) == ''
    assert brand_from_source_line(None, model='ZDP-9082') == ''
    assert brand_from_source_line('台式常温高速离心机 | G21000 | 2台 | 12,400.00元',
                                  model='G21000', category='离心机') == ''


def test_the_price_gate_still_refuses_the_residue_spelling():
    # 判据本身没动：残渣写法与真品牌仍然不是同一个（这条修复前后都成立）。
    assert brand_name_matches('喆图', '为喆图。') is False
    assert brand_name_matches('喆图', '喆图') is True
    assert brand_identity('为喆图。') == '为喆图。'


@pytest.mark.parametrize('candidate_brand, expected_reason', [
    ('为喆图。', 'brand_mismatch'),   # 修复前：候选品牌是残渣 → 真查到的喆图成交价被判品牌不符
    ('喆图', None),                    # 修复后：候选品牌已规范化 → 同一个价格行被收下
])
def test_the_price_verdict_changes_only_because_the_value_was_cleaned(candidate_brand,
                                                                     expected_reason):
    body = ('某医院医疗设备采购中标（成交）结果公告\n'
            '具体型号是ZDP-9082，品牌为喆图，数量2台，单价12,400.00元。\n')
    source = {'url': 'https://www.instrument.com.cn/netshow/SH100445/C227000.htm',
              'body_status': 'fetched', 'body_text': body, 'source_type': 'tender_document'}
    # 价格行自己的品牌写的是 ``喆图``：真查到的就是这个品牌的成交价。
    raw = {'excerpt': '具体型号是ZDP-9082，品牌为喆图，数量2台，单价12,400.00元。',
           'amount': 12400.0, 'amount_unit': '元', 'quantity': 2, 'model': 'ZDP-9082',
           'brand': '喆图', 'kind': 'award', 'amount_scope': 'unit', 'currency': 'CNY'}
    refusals = {}
    quote = validate_price(dict(raw), source, {'model': 'ZDP-9082', 'brand': candidate_brand},
                           refusals)
    if expected_reason is None:
        assert quote is not None
        assert refusals == {}
    else:
        assert quote is None
        assert refusals.get('reason_code') == expected_reason
        assert refusals.get('other_brand') == '喆图'


def test_the_lead_word_strip_is_a_known_side_effect_not_a_new_rule():
    """记录性用例：规范化会剥掉开头的引导字，``是德科技`` 这类真名会被改坏。

    这是 :func:`normalize_brand_value` 既有行为（本次只把它接上，一个字没改它），
    这里钉住现状，免得以后当成"本来就该这样"。放进名录/例外表是另一件事。
    """
    from backend.services.procurement_rules import normalize_brand_value
    assert normalize_brand_value('是德科技') == '德科技'
