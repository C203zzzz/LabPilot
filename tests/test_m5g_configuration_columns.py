"""迭代规范：多型号宽表**按列取**（摊平形态）、"参数名拿不到就留空"与框架不入配置。

判据改在 ``backend/services/product_configuration.py``。这批用例钉住三件事：

* **摊平的宽表**——同一张表各列首尾相连写在**同一行**里，渲染器为每一格生成
  ``[表格按列展开 行r列c] 型号 参数名 值``（实测 ``yiqi.com/product/detail_887574.html``，
  12456 字，整张 BJ 系列对照表就是这样）。按"列名就是型号"取目标列，产出必须**带参数名**，
  且一个字都不许把别的型号那一列拼进来。
* **参数名拿不到就留空**——表头第一格自己就是型号（``BWS-5 | BWS-10 | BWS-20 | BWS-0505``）
  的那种表没有"行标签列"，数据行的第一格是值。实测 ``yihengchina.com.cn`` 那份 PDF，
  就地重抽产出过 ``280×130×150 | 280×220×150``／``400W | 600W`` 这种**只有值没有参数名**
  的行；这一条改成整页留空（迭代规范 在 ``b3-opt-waterbath`` 的 ``BWS-10``／``BWS-0505`` 上实测到）。
* **页面框架不入配置**——问答句（``…详细技术参数吗？``）与页面按钮
  （``评论 分享 收藏 --> 对比 加入备选``）不是参数。
"""
from backend.services.product_configuration import (
    _column_spec_text,
    _expanded_column_pairs,
    product_configuration,
)

MODEL = 'BJ-2CD'
BRAND = '上海博迅'
URL = 'https://www.yiqi.com/product/detail_887574.html'

#: 照抄真实页面（``yiqi.com/product/detail_887574.html``）：整张对照表被摊成一行，
#: 后面紧跟渲染器为每一格生成的投影行。三列分别是 BJ-1CD／BJ-2CD／BJ-3CD。
FLAT_TABLE = '\n'.join([
    '上海博迅BJ-2CD净化工作台(双人单面垂直)',
    '技术参数：',
    '型号 | BJ-1CD | BJ-2CD | BJ-3CD 显示方式 | LCD | LCD | LCD 空气流向 | 垂直流 | 垂直流 | 垂直流',
    '[表格按列展开 行2列2] BJ-1CD 显示方式 LCD',
    '[表格按列展开 行2列3] BJ-2CD 显示方式 LCD',
    '[表格按列展开 行2列4] BJ-3CD 显示方式 LCD',
    '[表格按列展开 行3列2] BJ-1CD 空气流向 垂直流',
    '[表格按列展开 行3列3] BJ-2CD 空气流向 垂直流',
    '[表格按列展开 行3列4] BJ-3CD 空气流向 垂直流',
    '[表格按列展开 行5列2] BJ-1CD 洁净等级 100级（≥0.5μm过滤效率达到99.99％）',
    '[表格按列展开 行5列3] BJ-2CD 洁净等级 100级（≥0.5μm过滤效率达到99.99％）',
    '[表格按列展开 行5列4] BJ-3CD 洁净等级 100级（≥0.5μm过滤效率达到99.99％）',
    '[表格按列展开 行10列2] BJ-1CD 电源 830×700×495',
    '[表格按列展开 行10列3] BJ-2CD 电源 1300×700×495',
    '[表格按列展开 行10列4] BJ-3CD 电源 1800×700×495',
    '[表格按列展开 行16列2] BJ-1CD 荧光灯/紫外灯规格及数量 AC220±10V 50/60Hz',
    '[表格按列展开 行16列3] BJ-2CD 荧光灯/紫外灯规格及数量 AC220±10V 50/60Hz',
    '[表格按列展开 行16列4] BJ-3CD 荧光灯/紫外灯规格及数量 AC220±10V 50/60Hz',
    '您可能感兴趣的产品',
])

#: 照抄 ``yihengchina.com.cn`` 那份水浴锅样本的表头与两行数据：表头第一格就是型号，
#: 所以下面每行的第一格是**值**，页面上根本不存在参数名。
NO_FIELD_TABLE = '\n'.join([
    'BWS-5 | BWS-10 | BWS-20 | BWS-0505',
    '400W | 600W | 1200W | 1200W',
    '280×130×150 | 280×220×150 | 490×290×150 | 280×130×150',
    '396×250×260 | 396×330×260 | 600×390×260 | 450×395×260',
])


def source(url, body):
    return {'url': url, 'body_text': body, 'body_status': 'fetched',
            'source_type': 'unknown', 'title': 'x'}


def test_flat_wide_table_takes_the_column_of_the_model():
    """摊平宽表按列取：产出带参数名，值只是目标型号那一列。"""
    spec = product_configuration([source(URL, FLAT_TABLE)], MODEL, BRAND)
    assert spec is not None
    text = spec['text']
    assert '显示方式 | LCD' in text
    assert '空气流向 | 垂直流' in text
    assert '电源 | 1300×700×495' in text
    # 别的型号那一列的值一个字都不许进来
    assert '830×700×495' not in text and '1800×700×495' not in text
    # 三列值相同的行也要带参数名（公共前缀吃掉值的那条路）
    assert '荧光灯/紫外灯规格及数量 | AC220±10V 50/60Hz' in text


def test_flat_wide_table_keeps_the_field_name_of_every_row():
    rows, reason = _expanded_column_pairs(FLAT_TABLE, MODEL)
    assert reason == 'ok'
    # 五个参数行（行2 显示方式／行3 空气流向／行5 洁净等级／行10 电源／行16 荧光灯）
    assert len(rows) == 5
    for row in rows:
        name, _, value = row.partition(' | ')
        assert name and value, row
        assert name != value


def test_flat_wide_table_without_the_model_stays_empty():
    """型号对不上：留空，不许拿别的型号那一列顶上。"""
    rows, reason = _expanded_column_pairs(FLAT_TABLE, 'BXM-30R')
    assert rows == [] and reason == 'no-target-column'


def test_flat_wide_table_with_ambiguous_model_columns_stays_empty():
    """一行的两列都写着目标型号（合并单元格那种歧义）：整行不要。"""
    text = '\n'.join([
        '[表格按列展开 行2列2] BJ-2CD 显示方式 LCD',
        '[表格按列展开 行2列3] BJ-2CD 显示方式 LCD',
        '[表格按列展开 行3列2] BJ-2CD 空气流向 垂直流',
        '[表格按列展开 行3列3] BJ-2CD 空气流向 垂直流',
    ])
    rows, _reason = _expanded_column_pairs(text, MODEL)
    assert rows == []


def test_flat_wide_table_with_one_column_per_row_stays_empty():
    """同一行只有一列：没有第二条证据证明这是型号列，留空不猜。"""
    text = '\n'.join([
        '[表格按列展开 行2列2] BJ-2CD 显示方式 LCD',
        '[表格按列展开 行3列2] BJ-2CD 空气流向 垂直流',
    ])
    rows, reason = _expanded_column_pairs(text, MODEL)
    assert rows == [] and reason == 'one-column-rows'


def test_a_single_projection_row_is_not_a_table():
    """整页只有一条投影：一行是巧合，不是表。"""
    text = '技术参数：\n[表格按列展开 行2列2] BJ-2CD 显示方式 LCD'
    rows, reason = _expanded_column_pairs(text, MODEL)
    assert rows == [] and reason == 'no-projection'


def test_wide_table_without_a_field_name_column_is_refused():
    """表头第一格就是型号 → 这张表没有参数名列：判据返回 no-field-names。"""
    text, verdict = _column_spec_text(NO_FIELD_TABLE, 'BWS-10')
    assert text == '' and verdict == 'no-field-names'
    spec = product_configuration([source(
        'http://www.yihengchina.com.cn/uploads/upload/20250527/x.pdf',
        'BWS-5 | BWS-10 | BWS-20 | BWS-0505\n' + NO_FIELD_TABLE)], 'BWS-10', '一恒')
    assert spec is None, '只有值没有参数名的宽表不许写进配置'


def test_a_row_label_column_is_still_accepted():
    """正常宽表（第一格是行标签）不受上面那条影响：照旧按列取。"""
    text = '\n'.join([
        '内部校准 | QL1035A | QL2035A',
        '量程(g) | 120/31 | 120/51',
        '可读性(mg) | 0.1 | 0.1',
    ])
    rows, verdict = _column_spec_text(text, 'QL2035A')
    assert verdict == 'ok'
    assert '量程(g) | 120/51' in rows and '可读性(mg) | 0.1' in rows
    assert '120/31' not in rows


def test_page_chrome_never_reaches_the_configuration():
    """问答模板与页面按钮不进配置（M1 的判据继续生效）。"""
    body = '\n'.join([
        '上海博迅SW-CJ-1FD超净工作台',
        '有SW-CJ-1FD超净工作台的详细技术参数吗？ 评论 分享 收藏 --> 对比 加入备选',
        '技术参数：',
        '型号 | SW-CJ-1FD',
        '洁净等级 | 100级@≥0.5μm',
        '工作区尺寸 | 870×580×515',
        '评论 分享 收藏 --> 对比 加入备选',
    ])
    spec = product_configuration([source('https://www.instrument.com.cn/show/C217823.html', body)],
                                 'SW-CJ-1FD', '博迅')
    assert spec is not None
    text = spec['text']
    assert '详细技术参数吗' not in text
    for word in ('评论', '分享', '收藏', '加入备选'):
        assert word not in text, word
    assert '工作区尺寸 | 870×580×515' in text
