"""多型号宽表：报告那一行里**只留目标型号**的行／列。

用户批注：「**多型号的 就只保留对应型号的配置即可**，之前在扫描 pdf 获取配置的时候
有提到过，是只保留用户提出的型号」。PDF／结构化那条路早有这份实现
（``backend/services/evidence_ownership.project_table``），但它只喂给模型的提示词文档，
**产品自己出配置报告那条路（``report_configuration``）一次都没调过**——于是
``SPX-150BIII`` 那种 6 型号宽表印到报告里时，占掉 400 字上限的是表头加**别的型号**
（``SPX-70BⅢ``）那一行。

这一组测试钉住四件事：

1. **只删行／只删整格，不改字**（报告是证据，见 ``设计规范``）；
2. **单型号页面一行都不动**（非宽表页面的原行为）；
3. **退化路径**：推不出别的型号、或裁完一行不剩时**原样返回**，报告那一行绝不变空；
4. **型号边界**：``SPX-150B`` 与 ``SPX-150BIII`` 是两个方向的包含关系（闸门都判
   False）、``TBSPX-150BIII`` 有厂商前缀、``SPX-150B-Z``／``SPX-160B`` 数字不同——
   一个都不许认成目标型号，目标型号的罗马数字写法 ``SPX-150BⅢ`` 也不许认成别的型号。

夹具是 ``tests/fixtures/config-zip/`` 里 12 个**真实配置窗口的逐字片段**
（来源见 ``设计规范``）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.procurement_rules import contains_model
from backend.services.product_configuration import (
    MANUFACTURER_SOURCE, MODEL_SIBLING_PREFIX, PRODUCT_SOURCE,
    REPORT_CHARACTER_LIMIT, _fit_lines, _parameter_region_start,
    compressed_configuration, model_siblings, project_models,
    report_configuration)

FIXTURES = Path(__file__).parent / 'fixtures' / 'config-zip'
LABEL = '标称参数（非公告配置）：'


def real(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def report(text, model, source=MANUFACTURER_SOURCE, limit=REPORT_CHARACTER_LIMIT):
    return report_configuration({'configuration': text, 'model': model,
                                 'configuration_source': source}, limit=limit)


def body_of(line):
    """报告那一行里前缀之后的部分（比长度用）。"""
    return line.split(LABEL, 1)[1] if LABEL in line else line


def report_before_projection(text, model, limit=REPORT_CHARACTER_LIMIT):
    """**没有** ``project_models`` 这一道时的报告那一行。

    它与 ``report_configuration`` 走同一条路（同一条取材顺序：先切参数区、再压缩），
    只少最后那一步，所以两条输出可以直接比。
    只用于 ``MANUFACTURER_SOURCE``（本文件里 ``report`` 的默认值），前缀就是「厂家资料」。
    """
    start = _parameter_region_start(text.splitlines(), model)
    body = text if start is None else '\n'.join(text.splitlines()[start:])
    compact = compressed_configuration(body, model)
    excerpt = compact or body
    text_out, more = _fit_lines(excerpt, limit)
    return '厂家资料' + LABEL + text_out + ('…' if more else '')


def report_before_region_cut(text, model, source=MANUFACTURER_SOURCE,
                             limit=REPORT_CHARACTER_LIMIT):
    """改动**前**那条取材顺序的报告那一行（压缩优先，参数区只在压缩不出东西时兜底）。

    只用于"`_parameter_region_start` 返回 ``None`` 时逐字不变"这一条断言：
    起点找不到时切出来的 ``body`` 就是原文，两边是同一个输入。
    """
    compact = compressed_configuration(text, model)
    if compact:
        compact = project_models(compact, model)
    start = None if compact else _parameter_region_start(text.splitlines(), model)
    if compact:
        excerpt = compact
    elif start is not None:
        excerpt = '\n'.join(text.splitlines()[start:])
    else:
        excerpt = text
    text_out, more = _fit_lines(excerpt, limit)
    label = '厂家资料' if source == MANUFACTURER_SOURCE else '产品资料'
    return label + LABEL + text_out + ('…' if more else '')


#: 三个真实的多型号宽表 + 各自的目标型号。
WIDE_TABLES = [
    ('spx150biii-table.txt', 'SPX-150BIII'),      # 纵向长表：一行一个型号
    ('bdw86l490-table.txt', 'BDW-86L490'),        # 横向宽表：一列一个型号
    ('master-s15-table.txt', 'Master-S15'),       # 横向宽表 + 一行一个型号
]

#: 九个真实片段，页面里只有目标型号一台机器（不许裁掉任何一行）。
SINGLE_MODEL_PAGES = [
    ('kh20a-head.txt', 'KH20A'),
    ('kh20a-tail.txt', 'KH20A'),
    ('xka2200-full.txt', 'XKA-2200'),
    ('xz21kt-full.txt', 'XZ21K-T'),
    ('xz21kt-tail.txt', 'XZ21K-T'),
    ('h1850r-full.txt', 'H1850R'),
    ('h1850r-tail.txt', 'H1850R'),
    ('dw86l578s-spec.txt', 'DW-86L578S'),
    ('tu1901-spec.txt', 'TU-1901'),
]


# ---------------------------------------------------------------------------
# 一、三个真实宽表：报告那一行里不再出现别的型号
# ---------------------------------------------------------------------------

def test_the_spx_wide_table_no_longer_prints_the_other_models_row():
    """``SPX-150BIII``：修前 400 字里印的是**表头 + ``SPX-70BⅢ``（别的型号）+ 目标行**。

    修后同一份上限里只剩目标型号那一行（表头留着，``420×350×500`` 得有说明）。
    """
    source = real('spx150biii-table.txt')
    line = report(source, 'SPX-150BIII')

    assert '标 准 型 | SPX-150BⅢ | 220V/ 50HZ' in line, '目标型号那一行必须在'
    assert '| 500W |' in line
    for other in ('SPX-70BⅢ', 'SPX-250BⅢ', 'SPX-70BX', 'SPX-150BX', 'SPX-250BX'):
        assert other not in line, f'别的型号不该进报告：{other}'
    # 表头还在：报告里读到 ``420×350×500`` 时得知道那是什么
    assert '加热功率 Heat-ing power' in line
    # 行标签列（``标 准 型``）是目标行的一部分，没被切掉
    assert '标 准 型 | SPX-150BⅢ' in line

    # 修前确实是"表头 + 别的型号"占掉了这一段
    before = report_before_projection(source, 'SPX-150BIII')
    assert 'SPX-70BⅢ' in before
    assert 'SPX-70BⅢ' not in line
    assert len(body_of(line)) < len(body_of(before))


def test_the_bdw_wide_table_keeps_only_the_target_column():
    """``BDW-86L490``：表头 ``型号 | BDW-86L390 | BDW-86L490 | BDW-86L650 | BDW-86L770``。

    这是**横向**宽表：别的型号的参数不在别的行里，而在同一行的别的**列**里，
    按行删是删不掉的——必须按表头给的列索引裁。
    """
    source = real('bdw86l490-table.txt')
    line = report(source, 'BDW-86L490')

    for other in ('BDW-86L390', 'BDW-86L650', 'BDW-86L770'):
        assert other not in line, f'别的型号不该进报告：{other}'
    assert '型号 | BDW-86L490' in line
    # 目标型号**那一列**的值：490（容积）／332（重量）／16（320）（冻存架）
    # 别的列是 390/650/770、318/410/490、16（240）/20（400）/20（500）
    assert '名义容积 〔Liters〕 | 490' in line
    assert '重量 〔kg〕 | 332' in line
    assert '标准冻存架数量 (冻存盒数量) | 16（320）' in line
    # 行标签列（第 1 格）保留
    assert '温度范围 〔℃〕 | -50 ～ -86' in line


def test_the_master_s15_table_drops_the_other_row_and_the_other_columns():
    """``Master-S15``：列裁之后还有一行 ``产品型号 | 大流量型 | Master-S30 | …``。

    它自己不含目标型号，按列裁之后**目标列上落的是另一个型号**——必须再走一遍行判定
    才能删掉。
    """
    source = real('master-s15-table.txt')
    line = report(source, 'Master-S15')

    for other in ('Master-S15UF', 'Master-S15UV', 'Master-S15UVF',
                  'Master-S30', 'Master-S30UF', 'Master-S30UV', 'Master-S30UVF'):
        assert other not in line, f'别的型号不该进报告：{other}'
    assert '产品型号 | 标准型 | Master-S15' in line
    assert '电阻率 | 电阻率 | 18.2MΩ.cm@25℃' in line


# ---------------------------------------------------------------------------
# 二、不变量：只删行／只删整格，不改字
# ---------------------------------------------------------------------------

def _is_column_projection_of(line, candidates):
    """``line`` 是不是 ``candidates`` 里某一行**删掉若干整格**的结果。

    判据是"格序列是原行的子序列且逐字相等"（比格内容时去首尾空白——裁列会把整行的
    首尾空白 strip 掉，这是 ``compressed_configuration`` 本来就有的约定）。
    它同时保证了三件事：没有新造字符、没有改写保留的字符、顺序没变。
    """
    parts = [cell.strip() for cell in line.split('|')]
    for candidate in candidates:
        candidate_parts = [cell.strip() for cell in candidate.split('|')]
        if len(candidate_parts) <= len(parts):
            continue
        index = 0
        for part in candidate_parts:
            if index < len(parts) and part == parts[index]:
                index += 1
        if index == len(parts):
            return True
    return False


@pytest.mark.parametrize('name,model', WIDE_TABLES)
def test_projection_only_deletes_whole_lines_or_whole_cells(name, model):
    """投影结果的每一行，要么逐字等于压缩版里的某一行（行裁），要么是它删掉若干整格
    的结果（列裁）。**一个字都不改**——报告里的每个数值都要能在来源页原文里逐字对上。
    """
    compact = compressed_configuration(real(name), model)
    projected = project_models(compact, model)
    compact_lines = compact.splitlines()
    assert projected
    for line in projected.splitlines():
        assert line, '投影不该留下空行'
        assert line in compact_lines or _is_column_projection_of(line, compact_lines), line


@pytest.mark.parametrize('name,model', WIDE_TABLES)
def test_the_projection_keeps_the_target_model_and_its_row_labels(name, model):
    """裁完以后目标型号还在，行标签列也还在。"""
    compact = compressed_configuration(real(name), model)
    projected = project_models(compact, model)
    assert contains_model(projected, model)
    assert len(projected.splitlines()) <= len(compact.splitlines())


# ---------------------------------------------------------------------------
# 三、单型号页面（非宽表）：一行都不许被裁掉
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name,model', SINGLE_MODEL_PAGES)
def test_a_single_model_page_is_not_touched(name, model):
    """页面里只有目标型号一台机器时，``project_models`` 必须**原样返回**。

    这些片段里有 14 行的英文参数表、14 条的「字段名：值」参数表、报错般的
    ``AC220V ±22V 50/60Hz``／``(0.25～1ml)×12``——它们一格都不是型号。
    两个只含页面导航的片段压不出参数行（走保底），那一条也一并钉住。
    """
    source = real(name)
    compact = compressed_configuration(source, model)
    if compact is not None:
        assert model_siblings(compact.splitlines(), model) == [], '单型号页面推不出别的型号'
        assert project_models(compact, model) == compact
    # 报告那一行与"没有投影这一道"时逐字相同（压不出参数行时走的是保底路径）
    assert report(source, model) == report_before_projection(source, model)


def test_the_kh20a_page_keeps_every_parameter_row_it_had():
    """KH20A（用户报的那条坏例子）在压缩单里逐字不动，投影这一道一个字都没碰。"""
    source = real('kh20a-head.txt') + '\n' + real('kh20a-tail.txt')
    compact = compressed_configuration(source, 'KH20A')
    assert project_models(compact, 'KH20A') == compact
    line = report(source, 'KH20A')
    for row in ('最高转速 Max Speed | 18600r/min', '最大相对离心力 Max RCF | 23950×g',
                '重量 Weight | 20kg'):
        assert row in line, row


# ---------------------------------------------------------------------------
# 四、退化路径（硬要求）：推不出、或裁完不剩，原样返回
# ---------------------------------------------------------------------------

def test_a_short_model_code_is_left_to_the_old_behaviour():
    """短型号（``Q20`` 归一化后只有 ``q20`` 三个字符）推不出别的型号 → 一行都不裁。

    公共前缀在这么短的串上没有区分度：``q20`` 与 ``q10``／``q30`` 只共享 ``q``。
    ``tests/test_reliability_development.py:21`` 的 ``Q20``／````／``Q30`` 那组
    用例正属于这一类。代价是**漏裁**（报告里会多几行别的型号），不是误裁——
    真实语料里这种两字符型号出现在**单型号页**上（``KH20A``／``X100``），
    不裁没有代价。
    """
    text = ('技术参数\n型号 | Q20\n排气量 | 2\n'
            '型号 | Q10\n排气量 | 1\n型号 | Q30\n排气量 | 3\n')
    assert len('q20') <= MODEL_SIBLING_PREFIX
    assert model_siblings(text.splitlines(), 'Q20') == []
    assert project_models(text, 'Q20') == text


def test_a_numeric_leading_model_still_finds_its_sibling():
    """以数字开头的型号（``1300A2`` 对 ``1306A2``）仍然推得出来。

    前缀判据用的是**归一化写法的公共前缀**（``130``），不要求前缀是字母，
    所以这一档不会因为"型号以数字开头"而失效。
    """
    text = ('技术参数\n型号 | 1300A2\n排气压力 | 0.5MPa\n'
            '型号 | 1306A2\n排气压力 | 0.6MPa\n')
    assert model_siblings(text.splitlines(), '1300A2') == ['1306A2']
    out = project_models(text, '1300A2')
    assert '型号 | 1300A2' in out and '1306A2' not in out


def test_projection_returns_the_text_unchanged_when_every_line_would_be_dropped():
    """自推得到别的型号，但**目标型号一行都没有**时退回原文。

    这条是硬要求：压缩这条路不许因为"裁列失败"让报告那一行变成空白。
    """
    text = '标 准 型 | SPX-70BⅢ | 300W\n标 准 型 | SPX-250BⅢ | 800W\n'
    assert model_siblings(text.splitlines(), 'SPX-150BIII') == ['SPX-70BⅢ', 'SPX-250BⅢ']
    assert project_models(text, 'SPX-150BIII') == text


def test_the_report_line_is_never_emptied_by_the_projection():
    """上面那条退化路径走报告时：那一行仍有内容（是原文，不是空白）。"""
    text = '标 准 型 | SPX-70BⅢ | 300W\n标 准 型 | SPX-250BⅢ | 800W\n'
    line = report(text, 'SPX-150BIII')
    assert line.startswith('厂家资料' + LABEL)
    assert line != '厂家资料' + LABEL
    assert 'SPX-70BⅢ' in line, '裁不了就照原样印，不许变空'


@pytest.mark.parametrize('text', ['', '   ', '技术参数\n型号 | SPX-150BⅢ\n'])
def test_a_text_without_any_sibling_comes_back_verbatim(text):
    assert project_models(text, 'SPX-150BIII') == text


def test_non_string_inputs_pass_through():
    assert project_models(None, 'SPX-150BIII') is None
    assert project_models(123, 'SPX-150BIII') == 123
    assert project_models('技术参数', '') == '技术参数'
    assert project_models('技术参数', None) == '技术参数'


# ---------------------------------------------------------------------------
# 五、型号边界：同系列的别的型号、厂商前缀、罗马数字
# ---------------------------------------------------------------------------

def test_the_shorter_sibling_is_never_mistaken_for_the_target():
    """``SPX-150B`` 是 ``SPX-150BIII`` 的**真子串**，闸门本来就判 False。

    投影这一道必须与闸门同一个口径：``SPX-150B`` 是**别的型号**那一行，删掉它。
    """
    assert contains_model('SPX-150B', 'SPX-150BIII') is False
    text = ('技术参数\n'
            '标 准 型 | SPX-150BIII | 500W\n'
            '标 准 型 | SPX-150B | 300W\n')
    assert model_siblings(text.splitlines(), 'SPX-150BIII') == ['SPX-150B']
    out = project_models(text, 'SPX-150BIII')
    assert 'SPX-150BIII | 500W' in out
    assert '| SPX-150B | 300W' not in out


def test_the_roman_numeral_spelling_is_the_target_not_a_sibling():
    """原文写罗马数字 ``SPX-150BⅢ``（U+2162）：NFKC 之后就是目标型号，不许当别的型号。

    这一条错了的后果最严重：报告会把**目标型号自己那一行**删掉。
    """
    assert contains_model('SPX-150BⅢ', 'SPX-150BIII') is True
    text = ('技术参数\n'
            '标 准 型 | SPX-150BⅢ | 500W\n'
            '标 准 型 | SPX-70BⅢ | 300W\n')
    assert model_siblings(text.splitlines(), 'SPX-150BIII') == ['SPX-70BⅢ']
    out = project_models(text, 'SPX-150BIII')
    assert 'SPX-150BⅢ | 500W' in out
    assert 'SPX-70BⅢ' not in out


@pytest.mark.parametrize('cell', ['SPX-150B-Z', 'SPX-160B', 'X150BIII', 'SPX-150B'])
def test_a_different_number_is_never_read_as_the_target(cell):
    assert contains_model(cell, 'SPX-150BIII') is False


def test_a_vendor_prefixed_sibling_stays_untouched():
    """``TBSPX-150BIII``（``TB`` 是厂商代码还是型号号段，字符串上分不清）：
    ``contains_model`` 有意判 False（见 ``tests/test_model_match_signal.py``），
    而它的前缀 ``tb…`` 与 ``spx…`` 也不同 → 这一页**一条都不裁**。宁少勿错。
    """
    assert contains_model('TBSPX-150BIII', 'SPX-150BIII') is False
    text = ('技术参数\n'
            '标 准 型 | TBSPX-150BIII | 500W\n'
            '标 准 型 | SPX-150BIII | 500W\n')
    assert model_siblings(text.splitlines(), 'SPX-150BIII') == []
    assert project_models(text, 'SPX-150BIII') == text


def test_a_parameter_value_is_never_read_as_another_model():
    """参数值不是型号：``0205202000``／``4500r/min``／``AC220V ±22V 50/60Hz``／``100W``。

    结构与"同前缀"两条判据合起来挡住它们——这也是**不**用
    ``lead_alignment.model_tokens`` 的原因：那条尺子会把 ``220V``／``50HZ``／``R134a``
    也算成型号 token。
    """
    lines = ['技术参数', '型号 | XKA-2200', '订货号 | 0205202000', '最高转速 | 4500r/min',
             '电源 | AC220V ±22V 50/60Hz', '整机功率 | 100W', '重量 | 12kg',
             '转子容量 | (0.25～1ml)×12']
    assert model_siblings(lines, 'XKA-2200') == []
    assert project_models('\n'.join(lines), 'XKA-2200') == '\n'.join(lines)


def test_a_row_naming_both_models_is_kept():
    """一行**同时**写着目标与别的型号（对照行）：保留。

    这是与 ``project_table`` 有意不同的一处。``project_table`` 判"不继承"
    （喂模型时宁可少给）；报告里这一行是把目标与别的型号写在一起**对照**的，
    删掉就把目标型号的信息一起丢了。
    """
    text = ('技术参数\n'
            '标 准 型 | SPX-70BⅢ | 300W\n'
            '标 准 型 | SPX-150BⅢ | 500W\n'
            '标 准 型 | SPX-70BⅢ | SPX-150BⅢ | 500W\n')
    out = project_models(text, 'SPX-150BIII')
    assert '标 准 型 | SPX-70BⅢ | SPX-150BⅢ | 500W' in out, '对照行要留'
    assert '标 准 型 | SPX-70BⅢ | 300W' not in out, '纯别人的行要删'
    assert '标 准 型 | SPX-150BⅢ | 500W' in out


def test_the_heading_of_a_wide_table_is_kept():
    """表头（``型 号 Model | 型 号 Model | 电源电压 …``）不含型号：保留。

    与 ``project_table`` 的纵向分支有意不同的一处——它只输出"自己的行"，表头被丢掉；
    报告里丢了表头，``420×350×500`` 就没有说明。代价写在
    ``设计说明`` 里（表头约 200 字，占掉 400 字上限的一大半）。
    """
    source = real('spx150biii-table.txt')
    compact = compressed_configuration(source, 'SPX-150BIII')
    projected = project_models(compact, 'SPX-150BIII')
    assert '型 号 Model | 型 号 Model | 电源电压 （V） voltage' in projected
    assert projected.splitlines()[0] == '技术参数'


# ---------------------------------------------------------------------------
# 六、与 ``evidence_ownership.project_table`` 同一套判据
# ---------------------------------------------------------------------------

def test_the_column_rule_is_the_same_formula_as_project_table():
    """横向宽表保留的列索引就是 ``project_table`` 的那条公式：

    ``list(range(first_model)) + model_columns``——**目标型号列之前的所有列**当行标签，
    其他型号的列全部丢掉。这里直接把两条路的结果摆在一起比。
    """
    from backend.services.evidence_ownership import project_table

    rows = [['型号', 'BDW-86L390', 'BDW-86L490', 'BDW-86L650'],
            ['重量 〔kg〕', '318', '332', '410']]
    siblings = ['BDW-86L390', 'BDW-86L650']
    assert project_table(rows, 'BDW-86L490', siblings) == [
        ['型号', 'BDW-86L490'], ['重量 〔kg〕', '332']]

    text = '型号 | BDW-86L390 | BDW-86L490 | BDW-86L650\n重量 〔kg〕 | 318 | 332 | 410'
    assert project_models(text, 'BDW-86L490') == '型号 | BDW-86L490\n重量 〔kg〕 | 332'


def test_an_ambiguous_merged_header_falls_back_to_row_projection():
    """表头一格同时写着目标与别的型号（合并单元格的歧义）：**不裁列**。

    与 ``project_table:33-34`` 的 ``return []`` 同一个取舍——歧义不继承。
    行判定仍然生效：纯别人的行删掉。
    """
    text = ('型号 | SPX-70BⅢ/SPX-150BⅢ | SPX-250BⅢ\n'
            '重量 | 300 | 800\n'
            '标 准 型 | SPX-70BⅢ | 300W\n'
            '标 准 型 | SPX-250BⅢ | 800W\n')
    assert sorted(model_siblings(text.splitlines(), 'SPX-150BIII')) == ['SPX-250BⅢ', 'SPX-70BⅢ']
    out = project_models(text, 'SPX-150BIII')
    assert '型号 | SPX-70BⅢ/SPX-150BⅢ | SPX-250BⅢ' in out, '歧义表头整行不动'
    assert '重量 | 300 | 800' in out, '列没裁，数据行也整行不动'
    assert '标 准 型 | SPX-70BⅢ | 300W' not in out
    assert '标 准 型 | SPX-250BⅢ | 800W' not in out


def test_a_second_table_with_a_different_width_is_not_sliced():
    """页面里第二张表列数不同：不沿用上一张表的列索引去切它。"""
    text = ('型号 | BDW-86L390 | BDW-86L490\n'
            '重量 | 318 | 332\n'
            '报警 | 有 | 有 | 有 | 有\n')
    out = project_models(text, 'BDW-86L490')
    assert '型号 | BDW-86L490' in out
    assert '重量 | 332' in out
    assert '报警 | 有 | 有 | 有 | 有' in out, '列数不同的行不许切'


# ---------------------------------------------------------------------------
# 七、报告取材：参数区起点提到压缩之前（设计规范 §五 的最小修法）
# ---------------------------------------------------------------------------
#
# 改前的取材顺序是"压缩优先"：``compact = compressed_configuration(raw, model)``
# 压出东西就用压缩版，参数区起点只在**压缩不出东西**时才当兜底。
# 问题是"压缩成功"不等于"压缩对了"：整页原文里左边栏的推荐位每个自己就带
# 两行带量的参数行，压缩照样成功，成功出来的却全是**别台机器**。
# 修法把定位提到压缩之前：起点找得到就先切出来，再在切出来的这一段上压缩。

#: kaidasy 那页的左边栏形态：产品名带链接，紧跟三行「字段名：值」。
SIDEBAR_PAGE = (
    '首页 [https://www.kaidasy.com/]\n'
    '产品中心 [https://www.kaidasy.com/chanpin/]\n'
    'TG16G高速离心机 [https://www.kaidasy.com/gaosulixinji/11.html]\n'
    '最高转速：16600\n'
    '最大容量：400\n'
    '温控：常温\n'
    'KH23A通用高速离心机 [https://www.kaidasy.com/gaosulixinji/12.html]\n'
    '最高转速：20500\n'
    '最大容量：2000\n'
    '温控：常温\n'
    '技术参数\n'
    '型号 Model | KH20A\n'
    '最高转速 Max Speed | 18600r/min\n'
    '最大相对离心力 Max RCF | 24560×g\n'
    '最大容量 Max Capacity | 600ml\n'
)


def test_the_region_is_cut_before_compressing_when_the_start_is_found():
    """起点找得到时**先切一刀**：左边栏那台机器的参数不再抢走报告那 400 字。

    这三行跟着链接行走（``TG16G高速离心机 [https://…]`` + 三行「字段名：值」），
    ``_parameter_region_start`` 只认参数标题与"右格是量或本型号"的两格行，
    不被它们带走。
    """
    assert _parameter_region_start(SIDEBAR_PAGE.splitlines(), 'KH20A') is not None
    compact = compressed_configuration(SIDEBAR_PAGE, 'KH20A')
    assert compact is not None, '这一页现状就压得出东西——正是问题所在'
    assert '最高转速：16600' in compact, '压缩版里确实混进了左边栏那台机器'

    body = body_of(report(SIDEBAR_PAGE, 'KH20A'))
    assert 'KH20A' in body
    assert '24560' in body and '600ml' in body
    assert '最高转速：16600' not in body, '左边栏那台机器的转速不许进报告'
    assert '温控：常温' not in body


def test_without_a_region_start_the_report_line_is_unchanged():
    """起点找不到时输出与改前**逐字一致**。

    这一类是**结构上**等价，不是巧合：``body`` 就是原文 ``raw``，两边同一个输入。
    ``kh20a-head.txt`` 是实测起点为 ``None`` 的真片段（只有导航，没有参数区）。
    """
    source = real('kh20a-head.txt')
    assert _parameter_region_start(source.splitlines(), 'KH20A') is None
    assert report(source, 'KH20A') == report_before_region_cut(source, 'KH20A')


def test_a_page_without_a_region_start_still_uses_the_compacted_form():
    """起点找不到、但压缩压得出东西时（``h1850r-tail.txt``）：逐字与改前一致。"""
    source = real('h1850r-tail.txt')
    assert _parameter_region_start(source.splitlines(), 'H1850R') is None
    assert compressed_configuration(source, 'H1850R') is not None
    assert report(source, 'H1850R') == report_before_region_cut(source, 'H1850R')


def test_the_kaidasy_page_no_longer_prints_the_sidebar_machines():
    """kaidasy 那页的**整页真身**（11,995 字 < ``CONFIGURATION_MAXIMUM`` = 12,000）。

    这一页整页短于上限，``configuration_regions`` 直接返回全文，于是左边栏那 20 个
    推荐位的「最高转速／最大容量／温控」全在原料里。改前报告那 400 字装的全是它们，
    目标型号 ``KH20A`` 一个都没有（``24560``／``600ml`` 也没有）。
    """
    raw_text = real('kaidasy-kh20a-lixinji74.txt')
    assert len(raw_text) < 12000, '这一页的形态就是"整页短于 CONFIGURATION_MAXIMUM"'
    start = _parameter_region_start(raw_text.splitlines(), 'KH20A')
    assert start is not None
    assert '型号 Model | KH20A' in raw_text.splitlines()[start]

    body = body_of(report(raw_text, 'KH20A', source=PRODUCT_SOURCE))
    assert 'KH20A' in body, '报告那 400 字里必须出现目标型号'
    assert '24560' in body, '这台机器自己的最大相对离心力'
    assert '600ml' in body, '这台机器自己的最大容量'
    assert '温控：常温' not in body, '左边栏那 20 台机器的行不进报告'


def test_a_region_that_compacts_to_nothing_falls_back_to_the_region_text():
    """参数区压不出东西时用**参数区原文**（改前用的是整页压出来的页面字段）。

    ``RO DI400`` 那条"变好"就是这么来的：整页压出 151 字，内容是
    「法定代表人：未填／工商注册：1900/1/1／入驻时间：…」这些页面字段，
    参数区（``技术参数 :`` 起）被挤在 400 字之外；改后在参数区上压，压不出
    带量的参数行，于是退回参数区原文，目标型号才进得来。
    """
    raw_text = real('rodi400-app17-page.txt')
    lines = raw_text.splitlines()
    start = _parameter_region_start(lines, 'RO DI400')
    assert start is not None
    assert compressed_configuration(raw_text, 'RO DI400') is not None
    assert compressed_configuration('\n'.join(lines[start:]), 'RO DI400') is None

    line = report(raw_text, 'RO DI400', source=PRODUCT_SOURCE)
    assert 'RO DI400' in line
    assert '工商注册' not in line and '入驻时间' not in line
