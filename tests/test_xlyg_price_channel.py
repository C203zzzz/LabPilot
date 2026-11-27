"""本地成交明细库接进查价车道的回归（2026-10-05）。

钉住四件事：只取成交价（挂牌价不进）、型号精确匹配、库不在要如实说、行上带通道标记。
"""
import sqlite3

from backend.services import xlyg_local_db as xdb


def _item_row(model, brand, price, kind, date='2026-02-04', order='DD20260130000001', line=1):
    column = '协议价格（元）' if kind == 'negotiated' else '中标价'
    return (order, line, '高压灭菌器', '其他教学用仪器', brand, model, 1.0, price, price,
            column, kind, '灭菌器', date, '华南农业大学', '某某仪器有限公司',
            'https://gdegp.gds.edu.cn:8093/Upgrade/Publicity/ResultPublicity/x.aspx',
            f'{model} | {price:.2f} | 1.00 | {brand}',
            f'商品名称 | 产品名称 | 品牌 | 型号规格 | {column} | 数量',
            f'{model} | {price:.2f} | 1.00 | {brand}', 1)


def _build_db(tmp_path, rows):
    path = tmp_path / 'xlyg_prices.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(xdb.schema_sql())
    conn.executemany(
        'INSERT INTO xlyg_item (order_no, line_no, goods_name, product_name, brand, model, '
        'quantity, unit_price, amount, price_column, price_kind, category, deal_date, '
        'purchaser, supplier, detail_url, excerpt, table_header, price_excerpt, verified) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()
    return path


def test_only_deal_prices_are_returned(tmp_path):
    """直购单的目录金额（``list_price_direct``）是挂牌价，不许当成交价拿出来。"""
    path = _build_db(tmp_path, [
        _item_row('HVE-50', 'HIRAYAMA', 62000.0, 'negotiated'),
        _item_row('HVE-50', 'HIRAYAMA', 90000.0, 'list_price_direct', order='DD2'),
    ])
    out = xdb.local_price_rows('HVE-50', 'HIRAYAMA', database_path=path)
    assert len(out['rows']) == 1
    assert out['rows'][0]['unit_price'] == 62000.0
    assert out['rows'][0]['price_kind'] == 'negotiated'


def test_model_must_match_exactly(tmp_path):
    """``HVE-50`` 不能匹到 ``HVE-500``。"""
    path = _build_db(tmp_path, [
        _item_row('HVE-50', 'HIRAYAMA', 62000.0, 'negotiated'),
        _item_row('HVE-500', 'HIRAYAMA', 88000.0, 'negotiated', order='DD3'),
    ])
    out = xdb.local_price_rows('HVE-50', 'HIRAYAMA', database_path=path)
    assert [row['model'] for row in out['rows']] == ['HVE-50']


def test_library_brand_spellings_still_match(tmp_path):
    """库里的品牌写法更长（``梅特勒托利多METTLERTOLEDO``），不能因此漏掉同型号成交。"""
    path = _build_db(tmp_path, [
        _item_row('ME204E', '梅特勒托利多METTLERTOLEDO', 14000.0, 'auction_award'),
    ])
    out = xdb.local_price_rows('ME204E', '梅特勒-托利多', database_path=path)
    assert len(out['rows']) == 1
    assert out['rows'][0]['price_kind'] == 'auction_award'


def test_missing_database_is_reported_not_silent(tmp_path):
    """库不在时不能装作"这个型号没有成交价"。"""
    out = xdb.local_price_rows('HVE-50', 'HIRAYAMA', database_path=tmp_path / 'nope.sqlite3')
    assert out['rows'] == []
    assert '未生效' in out['note']


def test_rows_carry_the_channel_marker(tmp_path):
    path = _build_db(tmp_path, [_item_row('HVE-50', 'HIRAYAMA', 62000.0, 'negotiated')])
    row = xdb.local_price_rows('HVE-50', 'HIRAYAMA', database_path=path)['rows'][0]
    assert row['channel'] == 'xlyg_local_db'
    assert row['level'] == 'historical_configuration'
    assert row['amount_unit'] == '元' and row['amount_scope'] == 'unit'
    assert '单省' in row['scope_note'] or '单一省份' in row['scope_note']
    assert row['url'].startswith('https://gdegp.gds.edu.cn:8093/')
    assert row['date'] == '2026-02-04'
