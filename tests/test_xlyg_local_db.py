"""小零易购本地库的离线测试：**全部用合成样本，不打真实站点、不调用任何模型。**

合成样本的结构与真实页面一致（列表行 ``<div class="list-content">``、详情页商品表
的列名与页面自己的 ``DoView(this)`` 分支），人名／电话／地址都是编的。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.xlyg_prices import xlyg_router
from backend.services import xlyg_local_db as xdb

# ---------------------------------------------------------------------------
# 合成样本
# ---------------------------------------------------------------------------

NEGOTIATED_GUID = '02ebbf39-9501-447f-b22f-9f3b5adcc9eb'
DIRECT_GUID = '335b38e7-9bf0-47d0-a849-ef0c39846e92'
BILL_GUID = '848cfab7-8269-4d6f-a185-63af4bf4be93'
CANCELLED_GUID = 'aaaaaaaa-0000-0000-0000-000000000001'
SERVICE_GUID = 'ffffffff-0000-0000-0000-000000000000'
UNKNOWN_GUID = 'eeeeeeee-0000-0000-0000-000000000000'


def _row(guid, code_type, code1, code2, order_no, project, purchaser, supplier, when, state):
    return (
        f"<div class=\"list-content\" onclick=\"DoView(this)\" code='{guid}'"
        f" codeType='{code_type}' code1='{code1}' code2 ='{code2}'>"
        f'<div style="float:left;width:18%;"> {order_no} </div>'
        f'<div style="float:left;width:18%;" title=" {project}"> {project} </div>'
        f'<div style="float:left;width:19%;" title=" {purchaser}"> {purchaser} </div>'
        f'<div style="float:left;width:19%;" title=" {supplier}"> {supplier} </div>'
        f'<div style="float:left;width:16%;"> {when} </div>'
        f'<div style="float:left;width:10%;"> {state} </div>'
        '</div></div>')


LIST_HTML = (
    '<html><body><form>'
    '<input type="hidden" name="__VIEWSTATE" value="x" />'
    + _row(NEGOTIATED_GUID, '0', '100', '0', 'DD20260408000007', '生物安全柜', '华南农业大学',
           '广州康龙生物科技有限公司', '2026/4/8 16:21:59', '交易成功')
    + _row(DIRECT_GUID, '0', '100', '0', 'DD20260923000012', '落地式高速冷冻离心机', '仲恺农业工程学院',
           '广州博诗仪器设备有限公司', '2026/9/23 14:24:58', '交易成功')
    + _row(BILL_GUID, '3', '110', '0', 'CM20260920000098', '天平离心机采购', '广东科学技术职业学院',
           '广州义欣体育用品有限公司', '2026/9/20 09:35:08', '交易成功')
    + _row(CANCELLED_GUID, '4', '110', '0', 'CM20260101000002', '取消掉的竞价', '某学院',
           '某公司', '2026/1/1 09:00:00', '取消竞价')
    + _row(SERVICE_GUID, '1', '100', '1', 'DD20260101000001', '服务类单据', '某学院',
           '某公司', '2026/1/2 09:00:00', '交易成功')
    + _row(UNKNOWN_GUID, '9', '100', '0', 'DD20260101000003', '没见过的模板', '某学院',
           '某公司', '2026/1/3 09:00:00', '交易成功')
    + "ANP_checkInput(&#39;ctl00$ContentPlaceHolder1$UcPagination&#39;,3);"
    + '</form></body></html>')

#: 议价单：协议价格 43400 是成交价，金额 44800 是目录价。
NEGOTIATED_DETAIL = (
    '<html><body>'
    '<div class="title">议价 订单详情</div>'
    '订单名称： 生物安全柜 订单编号： DD20260408000007 成交总金额(元)： 43400.00'
    '<div class="title">一、商品信息</div>'
    '<table><tbody><tr>'
    '<th>商品名称</th><th>产品名称</th><th>品牌</th><th>型号规格</th>'
    '<th>金额（元）</th><th>协议价格（元）</th><th>数量</th><th>是否进口</th>'
    '<th>制造商</th><th>产地</th></tr><tr>'
    '<td>生物安全柜</td><td>其他教学用仪器</td><td>海尔</td><td>HR1780-IIA2（KY）</td>'
    '<td>44800.00</td><td>43400.00</td><td>1.00</td><td>否</td>'
    '<td>青岛海尔生物医疗科技有限公司</td><td>青岛</td>'
    '</tr></tbody></table>'
    '<div class="title">二、确定成交供应商</div>'
    '<table><tr><td>供应商：</td><td>广州康龙生物科技有限公司</td></tr>'
    '<tr><td>成交总金额（元）：</td><td>43400.00</td></tr></table>'
    '</body></html>')

#: 直购单：协议价格 0，成交价 = 金额（元）= 商品目录挂牌价，要单独标一类。
DIRECT_DETAIL = (
    '<html><body>'
    '<div class="title">直购 订单详情</div>'
    '订单名称： 落地式高速冷冻离心机 订单编号： DD20260923000012 成交总金额(元)： 95200.00'
    '<div class="title">一、商品信息</div>'
    '<table><tbody><tr>'
    '<th>商品名称</th><th>产品名称</th><th>品牌</th><th>型号规格</th>'
    '<th>金额（元）</th><th>协议价格（元）</th><th>数量</th><th>是否进口</th>'
    '<th>制造商</th><th>产地</th></tr><tr>'
    '<td>落地式高速冷冻离心机</td><td>其他教学用仪器</td><td>湖南恒诺</td><td>5-21R</td>'
    '<td>95200.00</td><td>0.00</td><td>1.00</td><td>否</td>'
    '<td>湖南恒诺仪器设备有限公司</td><td>湖南</td>'
    '</tr></tbody></table>'
    '<div class="title">二、确定成交供应商</div>'
    '<table><tr><td>供应商：</td><td>广州博诗仪器设备有限公司</td></tr>'
    '<tr><td>成交总金额（元）：</td><td>95200.00</td></tr></table>'
    '</body></html>')

#: 反拍／竞价单：成交单价只有「中标价」。限价 28656 而中标价 15000 —— 取错列高估近一倍。
BILL_DETAIL = (
    '<html><body>'
    '<div class="title">竞价 结果详情</div>'
    '采购项目编号:CM20260920000098'
    '<div class="title">一、商品信息</div>'
    '<table><tbody><tr>'
    '<th>商品名称</th><th>产品名称</th><th>品牌</th><th>型号规格</th><th>最高限价</th>'
    '<th>数量</th><th>是否进口</th><th>中标价</th><th>制造商</th><th>产地</th></tr><tr>'
    '<td>台式高速冷冻离心机</td><td></td><td>湘仪</td><td>H1850</td><td>28656.00</td>'
    '<td>1.00</td><td>否</td><td>15000.00</td><td>湖南湘仪</td><td>中国</td></tr>'
    '</tbody></table>'
    '<div class="title">二、确定成交供应商</div>'
    '<table><tr><td>中标供应商：</td><td>某某贸易有限公司</td></tr>'
    '<tr><td>中标价：</td><td>15000.00</td></tr></table>'
    '</body></html>')

DETAILS = {
    NEGOTIATED_GUID: {'html': NEGOTIATED_DETAIL, 'status': 'ok', 'note': ''},
    DIRECT_GUID: {'html': DIRECT_DETAIL, 'status': 'ok', 'note': ''},
    BILL_GUID: {'html': BILL_DETAIL, 'status': 'ok', 'note': ''},
}

WINDOW = ('2024-09-01', '2026-09-25')


def _build():
    rows, _pages = xdb.parse_list_page(LIST_HTML)
    return xdb.build(rows, DETAILS, window=WINDOW), rows


# ---------------------------------------------------------------------------
# 列表页
# ---------------------------------------------------------------------------

def test_page_count_comes_from_the_pager_text_not_from_anp_checkinput():
    """``ANP_checkInput(..., mv)`` 的 ``mv`` 只是页码框范围，**不是**总页数。

    实测站点同一页印着「总页数：64」而 ``mv=6``；拿 ``mv`` 当页数会漏掉九成数据。
    合成样本里让两者故意不同，钉住"以控件文字为准"。
    """
    html = ('<span>记录总数：<font color="blue"><b>640</b></font> 条&nbsp; '
            '总页数：<font color="blue"><b>64</b></font> 页&nbsp; 当前页：3</span>'
            "ANP_checkInput('ctl00$ContentPlaceHolder1$UcPagination',6);")
    totals = xdb.page_totals(html)
    assert (totals['records'], totals['pages'], totals['current']) == (640, 64, 3)
    assert xdb.parse_list_page(html)[1] == 64
    # 控件文字里没有页数时才退回 ANP 的值。
    assert xdb.parse_list_page(
        "ANP_checkInput('ctl00$ContentPlaceHolder1$UcPagination',3);")[1] == 3


def test_every_probe_keyword_has_a_category_mapping():
    """加词忘了配类别，会让那些行只能靠商品名文本猜类别——钉住这条。"""
    missing = [word for word in xdb.PROBE_KEYWORDS if word not in xdb.CATEGORY_OF]
    assert missing == []
    assert len(set(xdb.PROBE_KEYWORDS)) == len(xdb.PROBE_KEYWORDS)


def test_list_page_reads_the_six_columns_and_the_page_count():
    rows, pages = xdb.parse_list_page(LIST_HTML)
    assert pages == 3
    assert len(rows) == 6
    first = rows[0]
    assert (first['order_no'], first['project_name'], first['purchaser'],
            first['supplier'], first['deal_time'], first['state']) == (
        'DD20260408000007', '生物安全柜', '华南农业大学', '广州康龙生物科技有限公司',
        '2026/4/8 16:21:59', '交易成功')
    assert (first['code_type'], first['code1'], first['code2']) == ('0', '100', '0')


def test_service_and_unknown_code_types_are_never_fetched():
    """服务类（code2=1）那两套 FW 模板没验证过：不猜、不拼地址、不发请求。"""
    assert xdb.detail_template('0', '1') is None
    assert xdb.detail_template('3', '1') is None
    assert xdb.detail_template('9', '0') is None
    assert xdb.detail_template('0', '0').endswith('OrderResultPublicityView.aspx')
    assert xdb.detail_template('3', '0').endswith('ResultPublicityView.aspx')
    assert xdb.detail_template('4', '0').endswith('ResultPublicityView.aspx')
    assert xdb.detail_url({'code_type': '1', 'code2': '1', 'bill_guid': 'x', 'code1': 'y'}) is None


def test_deal_time_is_normalized_to_iso():
    assert xdb.normalize_deal_time('2026/9/23 14:24:58') == (
        '2026-09-23', '2026-09-23 14:24:58')
    assert xdb.normalize_deal_time('2026-09-23') == ('2026-09-23', '2026-09-23')
    assert xdb.normalize_deal_time('') == ('', '')


def test_window_is_the_recent_n_months():
    from datetime import date
    assert xdb.list_window(date(2026, 9, 25), 24) == ('2024-09-01', '2026-09-25')


# ---------------------------------------------------------------------------
# 成交单价读自哪一列
# ---------------------------------------------------------------------------

def test_negotiated_price_comes_from_the_agreement_column():
    body, _shaped = xdb.shape_body(NEGOTIATED_DETAIL)
    header, rows = xdb.goods_table(NEGOTIATED_DETAIL)
    picked = xdb.select_prices(header, rows, xdb.award_total(body))
    assert picked == [(0, '协议价格（元）', 43400.0)]


def test_direct_purchase_falls_back_to_the_amount_column():
    """直购单协议价格为 0：成交价 = 金额（元），但它记的是 `list_price_direct`。"""
    body, _shaped = xdb.shape_body(DIRECT_DETAIL)
    header, rows = xdb.goods_table(DIRECT_DETAIL)
    assert xdb.select_prices(header, rows, xdb.award_total(body)) == [(0, '金额（元）', 95200.0)]
    result, _ = _build()
    direct = next(item for item in result['items'] if item['order_no'] == 'DD20260923000012')
    assert direct['price_kind'] == 'list_price_direct'
    assert direct['unit_price'] == 95200.0


def test_bill_price_is_the_award_price_never_the_ceiling():
    body, _shaped = xdb.shape_body(BILL_DETAIL)
    header, rows = xdb.goods_table(BILL_DETAIL)
    assert xdb.select_prices(header, rows, xdb.award_total(body)) == [(0, '中标价', 15000.0)]
    result, _ = _build()
    bill = next(item for item in result['items'] if item['order_no'] == 'CM20260920000098')
    assert bill['unit_price'] == 15000.0
    assert bill['price_kind'] == 'auction_award'
    assert '28656' not in bill['price_excerpt']


def test_a_table_that_does_not_add_up_yields_no_rows_at_all():
    broken = BILL_DETAIL.replace('<td>15000.00</td>', '<td>14000.00</td>', 1)
    body, _shaped = xdb.shape_body(broken)
    header, rows = xdb.goods_table(broken)
    assert xdb.select_prices(header, rows, xdb.award_total(body)) is None


def test_html_parsing_and_the_products_text_parsing_agree():
    """两套彼此独立的解析必须给出同一组 (型号, 单价, 数量)。"""
    for html in (NEGOTIATED_DETAIL, DIRECT_DETAIL, BILL_DETAIL):
        body, _shaped = xdb.shape_body(html)
        header, rows = xdb.goods_table(html)
        picked = xdb.select_prices(header, rows, xdb.award_total(body))
        agree, why = xdb.cross_check(xdb.PLATFORM._quote_section(body), picked, header, rows)
        assert agree, why


# ---------------------------------------------------------------------------
# 建库：漏斗、类别、过滤
# ---------------------------------------------------------------------------

def test_build_drops_terminal_orders_and_unknown_templates():
    """服务类与"没见过的模板"都跳过，但**记法必须分开**：前者是有意不接、不是缺口。"""
    result, rows = _build()
    assert len(rows) == 6
    assert result['funnel']['unique_orders'] == 6
    assert result['funnel']['skipped_terminal'] == 1        # 取消竞价
    assert result['funnel']['skipped_service'] == 1         # 服务类（code2=1）
    assert result['funnel']['skipped_no_template'] == 1     # codeType=9 没见过
    assert result['funnel']['detail_fetched'] == 3
    assert result['funnel']['orders_with_price'] == 3
    assert result['funnel']['goods_rows_raw'] == 3
    assert result['funnel']['rows_kept'] == 3
    assert {item['order_no'] for item in result['items']} == {
        'DD20260408000007', 'DD20260923000012', 'CM20260920000098'}
    kinds = {gap['kind'] for gap in result['gaps']}
    assert 'unknown_template' in kinds
    # 服务类那条**一个字都不许进缺口表**：有意跳过被写成缺口，读的人会以为漏了数据。
    assert 'DD20260101000001' not in {gap['order_no'] for gap in result['gaps']}
    service = next(row for row in result['orders'] if row['order_no'] == 'DD20260101000001')
    assert service['fetch_status'] == 'skipped'
    assert '服务类' in service['skip_reason']
    cancelled = next(row for row in result['orders'] if row['order_no'] == 'CM20260101000002')
    assert cancelled['fetch_status'] == 'skipped'
    assert '取消' in cancelled['skip_reason']


def test_same_order_number_published_twice_is_merged_and_recorded(tmp_path):
    """同一个订单号出现在两个 bill_guid 上（先「终止采购」、后同一订单号重新发布并成交）。

    订单表以 order_no 为主键，只能留一条——**留最终态那条**，另一条整条记进缺口表，
    而且落盘行数必须等于内存结果行数（实测近 60 个月窗口里有 7 例这样的单）。
    """
    cancelled = {'bill_guid': 'T1', 'code_type': '1', 'code1': '100', 'code2': '0',
                 'order_no': 'DD20260101000009', 'project_name': '重新发布的单',
                 'purchaser': '某学院', 'supplier': '某公司',
                 'deal_time': '2026/1/1 09:00:00', 'state': '终止采购', 'keyword': ''}
    kept = {**cancelled, 'bill_guid': 'K1',
            'deal_time': '2026/1/9 09:00:00', 'state': '交易成功'}
    result = xdb.build([cancelled, kept], {}, window=WINDOW)
    assert result['funnel']['unique_orders'] == 2
    assert result['funnel']['merged_duplicate_order_no'] == 1
    assert [row['bill_guid'] for row in result['orders']] == ['K1']
    gaps = [gap for gap in result['gaps'] if gap['kind'] == 'duplicate_order_no']
    assert len(gaps) == 1
    assert 'T1' in gaps[0]['detail'] and 'K1' in gaps[0]['detail']

    path = tmp_path / 'xlyg_prices.sqlite3'
    xdb.write_database(path, result)
    conn = xdb.connect(path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM xlyg_order').fetchone()[0] == len(result['orders'])
    finally:
        conn.close()


def test_category_comes_from_the_items_own_text_with_the_keyword_as_fallback():
    result, _ = _build()
    by_order = {item['order_no']: item for item in result['items']}
    assert by_order['DD20260408000007']['category'] == '生物安全柜'
    assert by_order['DD20260408000007']['category_source'] == 'item_text'
    # 项目名是"天平离心机采购"，商品名是"台式高速冷冻离心机" → 离心机。
    assert by_order['CM20260920000098']['category'] == '离心机'


def test_no_price_reason_separates_the_three_real_causes():
    """三种"没价"必须分开说：没整单金额／型号列整列为空／明细与总额对不上。

    实测近 60 个月里 ``no_price`` 的 786 单「交易成功」全是**型号列整列为空**的服务性
    采购（站点把它们挂在物品类直购单上），不是解析退化——这句话必须能分辨出来。
    """
    header = ['商品名称', '型号规格', '协议价格（元）', '金额（元）', '数量']
    assert xdb.no_price_reason(header, [], None) == '页面没有印整单成交金额'
    service = [['招生宣传片拍摄制作', '', '4760.00', '4760.00', '1.00']]
    assert '型号规格' in xdb.no_price_reason(header, service, 4760.0)
    mismatch = [['离心机', 'H1850', '100.00', '200.00', '1.00']]
    assert '对不上' in xdb.no_price_reason(header, mismatch, 40000.0)


def test_every_kept_row_passes_the_products_own_price_gate():
    result, _ = _build()
    assert result['funnel']['rows_validate_price'] == 3
    for item in result['items']:
        assert item['verified'] == 1, item['verify_reasons']
        assert item['validate_price'] == 1, item['validate_note']
        assert item['order_total_consistent'] == 1
        assert item['model'] in item['excerpt']


def test_every_kept_row_has_its_order_in_the_order_table(tmp_path):
    """成功的单也必须进订单表——不然 ``/orders/{no}`` 只查得到失败的单。"""
    path, result = _database(tmp_path)
    orders = {row['order_no'] for row in result['orders']}
    assert len(result['orders']) == result['funnel']['unique_orders']
    for item in result['items']:
        assert item['order_no'] in orders, item['order_no']
    with _client(path) as client:
        payload = client.get('/api/v1/xlyg/orders/DD20260408000007').json()
        assert payload['order']['fetch_status'] == 'ok'
        assert payload['order']['award_total'] == 43400.0
        assert payload['order']['order_mode'] == '议价'
        assert [item['model'] for item in payload['items']] == ['HR1780-IIA2（KY）']


def test_a_missing_detail_page_is_a_named_gap_not_a_silent_zero():
    rows, _ = xdb.parse_list_page(LIST_HTML)
    result = xdb.build(rows, {}, window=WINDOW)
    assert result['items'] == []
    statuses = {row['order_no']: row['fetch_status'] for row in result['orders']}
    assert statuses['DD20260408000007'] == 'missing'
    assert statuses['CM20260101000002'] == 'skipped'


# ---------------------------------------------------------------------------
# 隐私
# ---------------------------------------------------------------------------

def test_a_phone_number_hidden_inside_an_order_number_is_not_personal_information():
    """``CM20180528000206`` 里藏着 ``18052800020``：正则不设左右边界就会误判。"""
    assert xdb.pii_residue('订单编号：CM20180528000206') == []
    assert xdb.pii_residue('DD20260923000012') == []
    assert '手机号' in xdb.pii_residue('供应商联系电话：13712345678')
    assert '手机号' in xdb.pii_residue('电话 13712345678。')


def test_the_products_redaction_removes_contact_blocks_before_anything_is_written():
    html = (NEGOTIATED_DETAIL.replace(
        '<div class="title">二、确定成交供应商</div>',
        '<div class="title">二、确定成交供应商</div>'
        '<table><tr><td>供应商联系人：</td><td>假联系人</td></tr>'
        '<tr><td>供应商联系电话：</td><td>13700000000</td></tr>'
        '<tr><td>供应商联系地址：</td><td>假地址一号楼</td></tr></table>')
        .replace('</body>',
                 '<div id="ctl00_ContentPlaceHolder1_three" class="info_details">'
                 '<div id="ctl00_ContentPlaceHolder1_shcoolTitle" class="title">'
                 '三、采购人的收货人信息和开票信息</div>'
                 '<table><tr><td>收货人：</td><td>假收货人</td></tr>'
                 '<tr><td>联系电话：</td><td>13900000000</td></tr></table></div></body>'))
    redacted, hits = xdb.redact(html.encode('utf-8'))
    assert hits == []
    assert '假联系人' not in redacted
    assert '13700000000' not in redacted
    assert '假收货人' not in redacted
    assert xdb.pii_residue(redacted) == []


# ---------------------------------------------------------------------------
# 落库 + API
# ---------------------------------------------------------------------------

def _database(tmp_path):
    result, _ = _build()
    path = tmp_path / 'xlyg_prices.sqlite3'
    xdb.write_database(path, result)
    return path, result


def test_database_round_trip_keeps_every_row(tmp_path):
    path, result = _database(tmp_path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM xlyg_item').fetchone()[0] == len(result['items'])
        assert conn.execute('SELECT COUNT(*) FROM xlyg_order').fetchone()[0] == len(result['orders'])
        columns = {row[1] for row in conn.execute('PRAGMA table_info(xlyg_item)')}
    finally:
        conn.close()
    assert {'brand', 'model', 'quantity', 'unit_price', 'deal_date', 'purchaser',
            'supplier', 'order_no', 'detail_url', 'excerpt'} <= columns


def _client(path):
    app = FastAPI()
    app.include_router(xlyg_router(path))
    return TestClient(app)


def test_api_finds_a_model_and_returns_every_promised_field(tmp_path):
    path, _ = _database(tmp_path)
    with _client(path) as client:
        response = client.get('/api/v1/xlyg/prices', params={'model': 'HR1780'})
        assert response.status_code == 200
        payload = response.json()
        assert payload['total'] == 1
        item = payload['items'][0]
        for field in ('brand', 'model', 'quantity', 'unit_price', 'deal_date', 'purchaser',
                      'supplier', 'order_no', 'detail_url', 'excerpt'):
            assert item[field] not in (None, ''), field
        assert item['model'] == 'HR1780-IIA2（KY）'
        assert item['unit_price'] == 43400.0
        assert item['deal_date'] == '2026-04-08'
        assert '代表不了全国行情' in payload['scope_note']


def test_api_filters_by_brand_category_and_price_kind(tmp_path):
    path, _ = _database(tmp_path)
    with _client(path) as client:
        assert client.get('/api/v1/xlyg/prices', params={'brand': '海尔'}).json()['total'] == 1
        assert client.get('/api/v1/xlyg/prices', params={'category': '离心机'}).json()['total'] == 2
        direct = client.get('/api/v1/xlyg/prices',
                            params={'price_kind': 'list_price_direct'}).json()
        assert direct['total'] == 1
        assert direct['items'][0]['model'] == '5-21R'
        # 未过闸门的行默认不出现；显式要才给。
        assert client.get('/api/v1/xlyg/prices',
                          params={'verified_only': False}).json()['total'] == 3


def test_instrument_only_drops_rows_that_are_not_lab_instruments(tmp_path):
    """整窗入库（设计规范）会把非仪器采购一起收进来，它们的 category 是空的。

    开关 ``instrument_only=true`` 就是给"只看实验室仪器/设备"这件事用的。
    """
    path, _ = _database(tmp_path)
    conn = xdb.connect(path)
    with conn:
        conn.execute("UPDATE xlyg_item SET category = '', category_source = '' "
                     "WHERE order_no = 'CM20260920000098'")
    conn.close()
    conn = sqlite3.connect(path)
    try:
        expected = conn.execute("SELECT COUNT(*) FROM xlyg_item WHERE verified = 1 "
                               "AND category IS NOT NULL AND category <> ''").fetchone()[0]
    finally:
        conn.close()
    assert 0 < expected < 3

    with _client(path) as client:
        assert client.get('/api/v1/xlyg/prices').json()['total'] == 3
        only = client.get('/api/v1/xlyg/prices', params={'instrument_only': True}).json()
        assert only['total'] == expected
        assert all(item['category'] for item in only['items'])
        assert only['filters']['instrument_only'] is True
        # 型号清单也要能只按仪器类统计：被清掉类别的那行的型号不再出现。
        models_all = {item['model'] for item
                      in client.get('/api/v1/xlyg/models').json()['items']}
        models_only = {item['model'] for item
                       in client.get('/api/v1/xlyg/models',
                                     params={'instrument_only': True}).json()['items']}
        assert models_only < models_all


def test_categories_endpoint_lists_the_covered_instrument_classes(tmp_path):
    path, _ = _database(tmp_path)
    with _client(path) as client:
        payload = client.get('/api/v1/xlyg/categories').json()
    names = {item['category'] for item in payload['items']}
    assert names == {'生物安全柜', '离心机'}
    assert all(item['models'] >= 1 and item['rows'] >= 1 for item in payload['items'])


def test_models_and_summary_endpoints(tmp_path):
    path, _ = _database(tmp_path)
    with _client(path) as client:
        models = client.get('/api/v1/xlyg/models', params={'q': 'H1850'}).json()['items']
        assert [item['model'] for item in models] == ['H1850']
        assert models[0]['min_price'] == models[0]['max_price'] == 15000.0
        summary = client.get('/api/v1/xlyg/summary').json()
        assert summary['items']['rows'] == 3
        assert summary['items']['models'] == 3
        assert summary['window'] == {'start': WINDOW[0], 'end': WINDOW[1]}
        assert summary['funnel']['rows_kept'] == 3


def test_order_endpoint_shows_why_an_order_produced_nothing(tmp_path):
    path, _ = _database(tmp_path)
    with _client(path) as client:
        payload = client.get('/api/v1/xlyg/orders/CM20260101000002').json()
        assert payload['order']['fetch_status'] == 'skipped'
        assert payload['items'] == []
        assert client.get('/api/v1/xlyg/orders/NOPE').status_code == 404


def test_a_missing_database_is_a_503_not_an_empty_result(tmp_path):
    with _client(tmp_path / 'absent.sqlite3') as client:
        response = client.get('/api/v1/xlyg/prices', params={'model': 'X'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'xlyg_database_missing'


@pytest.mark.parametrize('name', ['xlyg_order', 'xlyg_item', 'xlyg_gap', 'xlyg_meta'])
def test_the_sql_file_is_the_single_source_of_the_schema(tmp_path, name):
    conn = xdb.connect(tmp_path / 'schema.sqlite3')
    try:
        found = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                             (name,)).fetchone()
    finally:
        conn.close()
    assert found is not None
    assert Path(xdb.SCHEMA_PATH).exists()


def test_price_kinds_are_only_the_three_the_page_can_mean():
    result, _ = _build()
    assert {item['price_kind'] for item in result['items']} == {
        'negotiated', 'list_price_direct', 'auction_award'}
