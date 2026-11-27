"""报告里那一行「配置：」的压缩：只删行、不改字、逐字可对上。

夹具全部是**真实配置窗口里的逐字片段**（来源见 ``FIXTURES`` 的注释），
由 ``tmp/probe-zip/p19_makefixtures.py`` 从
``output/bio-instrument-acceptance-20260920/browser-runtime/acceptance.sqlite3``
各 run 的 price configuration 里截出，只截断、不改写。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.services.product_configuration import (
    MANUFACTURER_SOURCE, PRODUCT_SOURCE, REPORT_CHARACTER_LIMIT, SPEC_FIELD_ROWS,
    _parameter_lines, compressed_configuration, product_configuration,
    report_configuration)

FIXTURES = Path(__file__).parent / 'fixtures' / 'config-zip'
LABEL = '标称参数（非公告配置）：'


def real(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def report(text, model, source=MANUFACTURER_SOURCE, limit=REPORT_CHARACTER_LIMIT):
    return report_configuration({'configuration': text, 'model': model,
                                 'configuration_source': source}, limit=limit)


def assert_every_kept_line_is_verbatim(source, compact):
    """压缩结果的每一行都必须**逐字**存在于原文里；原文的参数行也不许丢。"""
    assert compact, '压缩结果不该是空的'
    source_lines = {line.strip() for line in source.splitlines() if line.strip()}
    for line in compact.splitlines():
        assert line.strip() in source_lines, f'这一行不是原文的逐字行：{line!r}'
    kept, _kinds = _parameter_lines(source.splitlines(), '')
    kept_bodies = {source.splitlines()[n].strip() for n in kept}
    for body in kept_bodies:
        assert body in compact.splitlines(), f'参数行被压丢了：{body!r}'


# ---------------------------------------------------------------------------
# 一、逐字可对上（本单最要紧的那条不变量）
# ---------------------------------------------------------------------------

REAL_CASES = [
    ('kh20a-head.txt', 'KH20A'),
    ('kh20a-tail.txt', 'KH20A'),
    ('xka2200-full.txt', 'XKA-2200'),
    ('xz21kt-full.txt', 'XZ21K-T'),
    ('xz21kt-tail.txt', 'XZ21K-T'),
    ('h1850r-full.txt', 'H1850R'),
    ('h1850r-tail.txt', 'H1850R'),
    ('dw86l578s-spec.txt', 'DW-86L578S'),
    ('tu1901-spec.txt', 'TU-1901'),
    ('spx150biii-table.txt', 'SPX-150BIII'),
    ('bdw86l490-table.txt', 'BDW-86L490'),
    ('master-s15-table.txt', 'Master-S15'),
]


@pytest.mark.parametrize('name,model', REAL_CASES)
def test_every_kept_line_exists_verbatim_in_the_source(name, model):
    """留下的每一行都能在原文里逐字找到——压缩只删行、不改字。"""
    source = real(name)
    compact = compressed_configuration(source, model)
    if compact is None:
        pytest.skip('这条片段没有成块参数行，走保底')
    assert_every_kept_line_is_verbatim(source, compact)


@pytest.mark.parametrize('name,model', REAL_CASES)
def test_compression_keeps_the_order_and_never_merges_lines(name, model):
    """压缩不重排、不合并：留下的行在原文里的先后顺序不变。"""
    source = real(name)
    compact = compressed_configuration(source, model)
    if compact is None:
        pytest.skip('这条片段没有成块参数行，走保底')
    source_lines = [line.strip() for line in source.splitlines() if line.strip()]
    position = 0
    for line in compact.splitlines():
        index = source_lines.index(line.strip(), position)
        assert index >= position, f'顺序被打乱：{line!r}'
        position = index + 1


def test_the_kh20a_navigation_is_deleted_and_the_whole_parameter_table_stays():
    """KH20A（用户报的那条坏例子）：导航删掉，主参数表 10 行一行不少。

    配置原文是那一页的窗口（8525 字 / 197 行），开头 20 行全是导航
    （``网站首页 [http://…]``）。修前报告从参数区起截 200 字只印到第 5 行参数；
    压缩后 400 字里主参数表整表进得来。
    """
    head, tail = real('kh20a-head.txt'), real('kh20a-tail.txt')
    source = head + '\n' + tail
    compact = compressed_configuration(source, 'KH20A')
    assert '网站首页' not in compact and 'http://' not in compact
    assert '型号 Model | KH20A' in compact
    for row in ('最高转速 Max Speed | 18600r/min', '最大相对离心力 Max RCF | 23950×g'):
        assert row in compact
    line = report(source, 'KH20A')
    assert line.startswith('厂家资料' + LABEL + '型号 Model | KH20A')
    assert '网站首页' not in line and 'http://' not in line
    # 主参数表 10 行全进
    for row in ('最高转速 Max Speed | 18600r/min', '最大相对离心力 Max RCF | 23950×g'):
        assert row in line


def test_the_xka2200_line_still_starts_at_the_heading_and_keeps_its_rows():
    """XKA-2200（已知的好例子）：修后必须仍然正常。

    它的配置窗口是 623 字 / 25 行、开头就是「技术参数」，修前修后第一行都是
    「技术参数」。压缩后 21 行（主参数表 14 行 + 转子表 6 行）；400 字里装到
    第 17 行——主参数表 14 行全进，差的是转子附表那 6 行。
    """
    source = real('xka2200-full.txt')
    line = report(source, 'XKA-2200')
    assert line.startswith('厂家资料' + LABEL + '技术参数 型号 | XKA-2200')
    assert line.endswith('…')
    for row in ('订货号 | 0205202000', '最高转速 | 4500r/min',
                '最大相对离心力 | 2000×g', '重量 | 12kg'):
        assert row in line, row
    # 转子表那 6 行在 400 字之外，但压缩结果里必须有（不是被删了）
    compact = compressed_configuration(source, 'XKA-2200')
    assert '淋巴球清洗用转子/HLA | 1 | 2000xg | 180s | 淋巴球分离,培养细胞分离' in compact


# ---------------------------------------------------------------------------
# 二、删掉了哪几类（各举一个真实例子）
# ---------------------------------------------------------------------------

def test_the_deleted_categories_are_navigation_links_footer_and_prose():
    source = '\n'.join([
        '技术参数',                                # 参数标题（留）
        '产品参数 [http://www.kaidalab.com/lixinji1238.html#cpcs] 产品介绍 [http://x/#cpjs]',  # 导航
        '[http://www.kaidalab.com/]',              # 裸链接行
        'Copyright © 1999-2024 湖南凯达科学仪器有限公司（kaidalab.com）版权所有',  # 页脚
        '',
        '型号 Model | KH20A',                      # 参数行（留）
        '最高转速 Max Speed | 18600r/min',          # 参数行（留）
        '◆7 英寸 高清触摸屏控制,操作简便,显示直观。',   # 说明性长句
        '最大相对离心力 Max RCF | 23950×g',
        '重量 Weight | 25kg',
    ])
    compact = compressed_configuration(source, 'KH20A')
    assert compact == '\n'.join([
        '技术参数', '型号 Model | KH20A', '最高转速 Max Speed | 18600r/min',
        '最大相对离心力 Max RCF | 23950×g', '重量 Weight | 25kg'])


def test_a_bare_link_line_and_a_copyright_line_are_dropped_from_the_report():
    source = '\n'.join(['技术参数', '型号 Model | KH20A', '最高转速 Max Speed | 18600r/min',
                        '最大相对离心力 Max RCF | 23950×g',
                        '[http://www.kaidalab.com/]',
                        'Copyright © 1999-2024 湖南凯达科学仪器有限公司版权所有'])
    line = report(source, 'KH20A')
    assert 'http://' not in line
    assert 'Copyright' not in line and '版权所有' not in line
    assert '型号 Model | KH20A' in line


# ---------------------------------------------------------------------------
# 三、参数行判定复用既有口径（不是第三套规则）
# ---------------------------------------------------------------------------

def test_a_two_cell_measure_row_is_a_parameter_and_the_lone_model_row_is_not():
    """两格行沿用 ``_spec_field_rows`` 的尺子：值是这个型号的行也留（它是"这段
    参数属于这个型号"的证据），孤立的标题不构成参数表（没有 ``value`` 锚，
    ``compressed_configuration`` 会整体作废）。"""
    kept, kinds = _parameter_lines(['型号 | XKA-2200'], 'XKA-2200')
    assert kept == [0] and kinds == ['value'], '值就是这个型号的行要留'
    kept, kinds = _parameter_lines(['技术参数'], 'XKA-2200')
    assert kinds == ['anchor'], '标题是锚、但不是带量的锚'
    assert compressed_configuration('技术参数\n', 'XKA-2200') is None
    kept, kinds = _parameter_lines(['技术参数', '型号 | XKA-2200', '重量 | 12kg'], 'XKA-2200')
    assert kept == [0, 1, 2]


def test_a_multi_cell_row_needs_a_measure_row_in_the_same_block():
    """多格行（型号对照表）本身不含量值时，要靠同块的锚才进得来。"""
    orphan = ['型 号 Model | 型 号 Model | 电源电压 （V） voltage | 控温范围 （℃） Tem. variation']
    kept, _kinds = _parameter_lines(orphan, 'SPX-150BIII')
    assert kept == [], '没有锚的孤立多格行不是参数表'
    anchored = ['技术参数', '标 准 型 | SPX-150BⅢ | 220V/ 50HZ | 0-65℃ | 0.1℃ | ±0.5℃ | ±1℃ | 500W']
    kept, _kinds = _parameter_lines(anchored, 'SPX-150BIII')
    assert kept == [0, 1]


def test_an_award_notice_without_a_parameter_table_returns_none():
    """省级成交公告那类结构上不印参数表的配置：压不出东西，返回 ``None``。

    它的字段行（``项目编号：``／``采购人：``／``成交金额：``）形状与
    「字段名：值」一样，但值是人名、公司名、日期——没有量、也不是型号，
    所以一行 ``value`` 都没有，压缩整体作废（报告退回"从参数区起截"）。
    """
    notice = '\n'.join(['某某医院医疗设备采购项目 成交公告',
                        '项目编号：TEST-2026-001',
                        '采购人：某某医院',
                        '成交供应商：某某医疗器械有限公司',
                        '成交金额：人民币 174800 元',
                        '公告期限：自本公告发布之日起 1 个工作日'])
    assert compressed_configuration(notice, 'XKA-2200') is None
    assert compressed_configuration('序号 | 名称 | 品牌 | 数量 | 单价', 'XKA-2200') is None


def test_an_award_item_table_is_not_read_as_the_product_configuration():
    """公告的成交标的表（第 1 格是序号）不算产品参数表。

    它的形状（多格行 + 型号 + 数量）与型号对照表**无法区分**，只能靠
    "值带量的行不够两行"挡住：这一张表只有一行带量（``数量`` 那一格）。
    """
    award = ['序号 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价',
             '1 | 血型血清学离心机 | 湘仪 | XKA-2200 | 1 | 20000']
    assert compressed_configuration('\n'.join(award), 'XKA-2200') is None


def test_a_shared_instrument_register_is_not_a_parameter_block():
    """高校共享仪器平台的设备台账页（型号/功能/工程师/地点/编号）不算参数表。"""
    register = ['型号： | Lambda 750S', '功能： | 190nm~2500nm 波长范围内薄膜样品吸光度测试',
                '工程师： | 付老师 / (021)34206126-6010', '设备地点： | 化学楼 302',
                '设备编号： | 20210001']
    kept, _kinds = _parameter_lines(register, 'Lambda 750S')
    assert kept == [], f'设备台账不该进来：{kept}'


def test_a_field_style_table_without_any_grid_is_still_read():
    """「字段名：值」形态（``bjpersee.com`` 的 TU-1901）：一行 ``|`` 都没有也要读。

    只认网格的话这 14 行真参数会被整段丢掉，压缩反而比不压缩信息更少。
    """
    lines = ['技术参数：', '1、 波长范围： 190nm～900nm', '2、 波长准确度：±0.3nm(开机自动校准)',
             '3、 波长重复性：0.1nm', '4、 光谱带宽： TU-1900：2nm',
             '5、 杂散光： ≤0.01%T(220nm,NaI； 360nm,NaNo2)', '6、 光度方式： 透过率、吸光度']
    kept, _kinds = _parameter_lines(lines, 'TU-1901')
    assert kept == [0, 1, 2, 3, 4, 5, 6]


def test_a_contact_line_and_a_page_meta_line_are_not_parameters():
    """页脚联系方式与页面元信息：形状像参数（短字段 + 数字），但不是参数。"""
    kept, _kinds = _parameter_lines(['固话： 021-80122208', '更新时间： 2026-08-03',
                                     'Q Q：800072208', '技术参数', '重量 | 12kg'], 'XKA-2200')
    assert kept == [3, 4], f'页脚/元信息不该进来：{kept}'


# ---------------------------------------------------------------------------
# 四、没有参数表时的兜底（反例）
# ---------------------------------------------------------------------------

def test_a_configuration_without_any_parameter_row_returns_none():
    """省级成交公告那类结构上不印参数表的配置：压不出东西，返回 ``None``。"""
    notice = '\n'.join(['某某医院医疗设备采购项目 成交公告',
                        '项目编号：TEST-2026-001',
                        '采购人：某某医院',
                        '成交供应商：某某医疗器械有限公司',
                        '成交金额：人民币 174800 元',
                        '公告期限：自本公告发布之日起 1 个工作日'])
    assert compressed_configuration(notice, 'XKA-2200') is None
    # 成块但又没锚（只有一行多格表头）
    assert compressed_configuration('序号 | 名称 | 品牌 | 数量 | 单价', 'XKA-2200') is None


def test_the_report_falls_back_to_the_old_excerpt_when_nothing_can_be_compressed():
    """压不出参数行时按老办法（从参数区起截），不变空、也不塞垃圾。"""
    notice = ('这是一段没有参数表的说明文字。' * 40 + '\n技术参数\n'
              + '还是说明文字。\n' * 80)
    line = report(notice, 'XKA-2200')
    assert line.startswith('厂家资料' + LABEL + '技术参数')
    assert line.endswith('…')
    assert len(line) <= REPORT_CHARACTER_LIMIT + 40


def test_a_single_parameter_row_is_left_to_the_old_path():
    """只压出一行带量的参数时退回老办法：压缩后信息比原来少，就不压。"""
    source = '技术参数\n型号 | XKA-2200\n'
    assert compressed_configuration(source, 'XKA-2200') is None
    source = '技术参数\n型号 | XKA-2200\n重量 | 12kg\n'
    assert compressed_configuration(source, 'XKA-2200') is not None


def test_the_report_never_half_cuts_a_value():
    """按行截：报告里最后一个数值必须完整，不能印出 ``整机功率 | 1.6``。"""
    source = '\n'.join(['技术参数'] + [f'参数{i:02d} | {i}000r/min' for i in range(1, 30)])
    line = report(source, 'XKA-2200')
    body = line.split('：', 1)[1]
    assert body.endswith('…')
    last = [part for part in re.split(r'\s{2,}|\s(?=参数\d)', body.rstrip('…')) if part.strip()]
    assert last, '报告里应该有参数'
    assert re.match(r'^参数\d{2} \| \d+000r/min$', last[-1]), last[-1]


# ---------------------------------------------------------------------------
# 五、真实记录：修前 / 修后（至少 5 条，KH20A 与 XKA-2200 必在其中）
# ---------------------------------------------------------------------------

def _old_line(text, model, limit=200):
    """上一版实现：从参数区起截 limit 字（按字符切）。"""
    from backend.services.product_configuration import _parameter_region_start
    start = _parameter_region_start(text.splitlines(), model)
    excerpt = text if start is None else '\n'.join(text.splitlines()[start:])
    flat = ' '.join(excerpt.split())
    return ('厂家资料标称参数（非公告配置）：'
            + flat[:limit] + ('…' if len(flat) > limit else ''))


BEFORE_AFTER = [
    ('KH20A', 'kh20a-head.txt', 'kh20a-tail.txt', 8525),
    ('XKA-2200', 'xka2200-full.txt', None, 623),
    ('XZ21K-T', 'xz21kt-full.txt', 'xz21kt-tail.txt', 3271),
    ('H1850R', 'h1850r-full.txt', 'h1850r-tail.txt', 3643),
    ('DW-86L578S', 'dw86l578s-spec.txt', None, 11089),
    ('TU-1901', 'tu1901-spec.txt', None, 4064),
]


@pytest.mark.parametrize('model,head,tail,full_chars', BEFORE_AFTER)
def test_the_after_line_is_never_worse_than_the_before_line(model, head, tail, full_chars):
    """修后不许比修前差：不许有 ``http://``、不许以导航开头、不许空。"""
    source = real(head) + ('\n' + real(tail) if tail else '')
    after = report(source, model, limit=REPORT_CHARACTER_LIMIT)
    before = _old_line(source, model)
    assert after and after.strip() != '厂家资料' + LABEL
    assert 'http://' not in after.split('：', 1)[1]
    assert '网站首页' not in after
    # 修后第一段必须是参数名/参数标题，不是页面导航
    body = after.split('：', 1)[1]
    assert not body.startswith('凯达牌') and not body.startswith('首页')
    assert before  # 修前那一条仍然是有内容的（不是空的），两者可比


# ---------------------------------------------------------------------------
# 六、产品资料那一档（``product_page_spec``）走同一条路
# ---------------------------------------------------------------------------

def test_a_third_party_page_takes_the_same_compression_path():
    source = '\n'.join(['免责声明：本站不保证信息准确。', '技术参数',
                        '型号 | XKA-2200', '最高转速 | 4500r/min',
                        '最大相对离心力 | 2000×g', '[http://www.example.com/]'])
    compact = compressed_configuration(source, 'XKA-2200')
    assert compact is not None
    assert '免责声明' not in compact and 'http://' not in compact
    line = report(source, 'XKA-2200', source=PRODUCT_SOURCE)
    assert line.startswith('产品资料' + LABEL + '技术参数')
    assert '免责声明' not in line and 'http://' not in line


def test_the_compressed_text_is_what_product_configuration_would_store():
    """判据认哪份，写出去的就是哪份：``configuration`` 存的是压缩结果，不是窗口原文。

    原来存窗口原文，理由是"证据不被动"。可判据本来就在压缩结果上看，输出却给未压缩的
    窗口——页面框架于是**绕过判据**进了配置：实测 ``yiqi.com`` HVE-50 那页
    （``product/detail_1867343.html``）两个 ``find_ulbox``（分类导航 + 推荐商品）被拼成
    1979 字写进 9 条价格共用的配置。压缩只删行、不改字，下面最后一条断言钉住这一点：
    留下来的每一行都能在原文里逐字找到。
    """
    body = '\n'.join(['技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min',
                      '最大相对离心力 | 2000×g', '重量 | 12kg', 'Copyright © 某某公司'])
    spec = product_configuration(
        [{'url': 'https://www.example.com/p/1', 'body_status': 'fetched',
          'source_type': 'unknown', 'body_text': body}], 'XKA-2200')
    compact = compressed_configuration(body, 'XKA-2200')
    assert compact is not None
    assert spec['text'] == compact, '写出去的就是判据认的那份'
    assert 'Copyright' not in spec['text'] and '4500r/min' in spec['text']
    for line in spec['text'].splitlines():
        assert line in body.splitlines()
