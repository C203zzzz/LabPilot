"""同一条公告的同一笔成交，只能是一条价格记录。

实测缺陷（``设计规范-改后效果评测.md`` §5.1）：``task-271ebe2c1de7`` 与
``task-0a7f1ff94935`` 的 ``run['prices']`` 各出现**同 URL、同 17480 元**两条记录，
于是 ``price_decision.transaction_count`` 报 2，而同一个对象的 ``summary`` 写的是
「查到1条同型号历史成交」。

根因是 ``_price_key()`` 把「两次提取填的字段完整度不同」当成了两笔不同的成交：

    271 [0] key=('YXZC2026-G1-00082-HCZB-0013', '4包', '血型血清学离心机', 'xka2200', 17480.0, '2026-03-25')
    271 [1] key=('YXZC2026-G1-00082-HCZB-0013', '',     '',                'xka2200', 17480.0, 'unknown')

``_price_key`` 是「同一次扫描内的增删去重」，不是「同一笔成交的判重」；判重改由
``_same_transaction`` 负责：两条记录在**双方都写明的**字段上全部一致、且 URL、
型号、单价相同，才是同一笔。

本文件同时钉住另一头：同 URL 上**合法的多条分项价**（不同型号、不同单价、双方都
写明的不同包号、不同口径）必须一条都不许少。
"""
from backend.services.procurement_research import (
    _merge_price_rows, _merged_prices, _price_key, _same_transaction,
)
from backend.services.procurement_rules import price_decision

NOTICE = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
EXCERPT = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'
BASELINE = {'brand': '湘仪', 'model': 'XKA-2200', 'supplier_price': 38000.0, 'currency': 'CNY'}


def _readings():
    """§5.1 逐字记录的两次提取：一次带包号／品名／日期，一次这几格是空的。"""
    full = {'url': NOTICE, 'project_id': 'YXZC2026-G1-00082-HCZB-0013', 'lot': '4包',
            'item': '血型血清学离心机', 'model': 'XKA-2200', 'unit_price': 17480.0,
            'amount': 17480, 'quantity': 1, 'date': '2026-03-25', 'kind': 'award',
            'amount_scope': 'unit', 'currency': 'CNY', 'excerpt': EXCERPT}
    sparse = {**full, 'lot': '', 'item': '', 'date': 'unknown'}
    return full, sparse


def _row(**overrides):
    row = dict(url=NOTICE, project_id='P-1', lot='4包', item='血型血清学离心机',
               model='XKA-2200', unit_price=17480.0, amount=17480, quantity=1,
               date='2026-03-25', kind='award', amount_scope='unit', currency='CNY')
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 缺陷本身
# ---------------------------------------------------------------------------

def test_the_two_strict_keys_really_do_differ():
    """根因：``_price_key`` 把两格空白当成两个值。这条钉住的是**改之前**的行为。"""
    full, sparse = _readings()
    assert _price_key(full) != _price_key(sparse)


def test_two_readings_of_one_transaction_are_one_row():
    full, sparse = _readings()
    assert _same_transaction(full, sparse)
    assert len(_merge_price_rows([full, sparse])) == 1
    assert len(_merged_prices([full], [sparse])) == 1


def test_the_row_that_survives_is_the_one_that_states_more():
    """保留信息更全的那次读数：包号／品名／日期不能因为去重而丢掉。"""
    full, sparse = _readings()
    merged = _merge_price_rows([sparse, full])
    assert len(merged) == 1
    assert merged[0]['lot'] == '4包'
    assert merged[0]['item'] == '血型血清学离心机'
    assert merged[0]['date'] == '2026-03-25'


def test_the_decision_no_longer_contradicts_its_own_summary():
    full, sparse = _readings()
    accepted = [full, sparse]
    decision = price_decision(accepted, BASELINE)
    assert decision['transaction_count'] == 2, '这是改前的状态：同一笔被数成两笔'
    assert '2条' in decision['summary'] or '找到2条' in decision['summary']

    merged = _merge_price_rows(accepted)
    decision = price_decision(merged, BASELINE)
    assert decision['transaction_count'] == 1
    assert len(decision['references']) == 1
    assert '1条' in decision['summary'] or '找到1条' in decision['summary']


def test_a_verbatim_repeat_is_still_dropped():
    """严格键原来的职责不许退化。"""
    row = _row()
    assert len(_merge_price_rows([row, dict(row)])) == 1


# ---------------------------------------------------------------------------
# 反例：同 URL 上合法的多条分项价，一条都不许合并
# ---------------------------------------------------------------------------

def test_a_second_model_at_the_same_url_stays_two():
    """同一公告里两台不同型号的仪器，各有各的单价。"""
    first = _row()
    second = _row(model='XZ21K-T', item='台式冷冻高速离心机', unit_price=23000.0, amount=23000)
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_a_second_unit_price_at_the_same_url_stays_two():
    """同一型号分两个包，包价不同。"""
    first = _row(lot='4包', unit_price=17480.0, amount=17480)
    second = _row(lot='5包', unit_price=20800.0, amount=20800)
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_two_stated_lots_at_one_price_stay_two():
    """**最关键的反例**：同型号、同单价，但两次读数都写明了包号且不同。

    只按 URL（或 URL+单价+型号）抹平的写法会在这里少算一笔，所以判重必须看
    「双方都写明的字段」是否一致。
    """
    first = _row(lot='4包')
    second = _row(lot='5包', excerpt='包别5：湘仪 XKA-2200 17480')
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_a_stated_package_total_is_not_the_same_claim_as_a_unit_price():
    """数字相同也不一定是同一笔：一条是单台价，一条是整包价。"""
    first = _row(amount_scope='unit')
    second = _row(amount_scope='package_total')
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_two_stated_dates_stay_two():
    first = _row(date='2026-03-25')
    second = _row(date='2026-04-01')
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_two_stated_quantities_stay_two():
    first = _row(quantity=1)
    second = _row(quantity=2, amount=34960)
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_different_pages_are_never_merged():
    first = _row()
    second = _row(url='http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202604/t20260401_1.htm')
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_a_row_without_a_unit_price_is_never_merged_away():
    """没有价格的记录不是「同一笔价格的另一种写法」。"""
    first = _row()
    second = _row(unit_price=None)
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2


def test_the_known_limit_is_pinned():
    """已知边界，写在这里是为了让它可见、可改。

    两次读数**都没写明**包号／品名时，判据手里的证据无法区分
    「一笔成交的两次读数」与「同价的真实两笔」；本改动按前者合并。真实缺陷数据
    （§5.1 的两组）都属于「一次写明、一次空白」，所以未触发这一格。
    真要收窄，入口是 ``_TRANSACTION_FIELDS`` 与 ``_same_transaction``：需要一条
    能定位到正文某一行的证据（``excerpt`` 的字符位置），而不是再加一个字段。
    """
    first = _row(lot='', item='', date='unknown')
    second = _row(lot='', item='', date='unknown', excerpt='湘仪 XKA-2200 17480')
    assert _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 1


def test_junk_rows_are_tolerated():
    assert _merge_price_rows([None, 'x', _row()]) == [_row()]
    assert _merged_prices(None, None) == []


# ---------------------------------------------------------------------------
# 第二副面孔：表格解析通道与模型抽取通道各出一条（设计规范）
# ---------------------------------------------------------------------------

A283_NOTICE = 'http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/t20260812_27118698.htm'
A283_EXCERPT = ('1 | 北京睿辰生物医疗科技有限公司 | A2型生物安全柜； 倒置显微镜； '
                'IVF用体式显微镜； 荧光倒置显微镜； 实时荧光定量PCR系统； 超速离心机； '
                '酶标仪； 洗板机； 多功能酶标仪 | 贝晶； 徕卡； 徕卡； 徕卡； 臻合智造； '
                '贝克曼； 帝肯； 帝肯； 帝肯 | Mars1500； DMi1； Ivesta 3； DMi8； '
                'ZHFluoroX7 Pro； Optima MAX-XP； Sunrise； HydroFlex； Spark cn | '
                '11台； 2台； 2台； 2台； 1套； 1台； 1台； 2台； 1台 | '
                '48000； 55000； 49000； 780000； 780000； 680000； 88000； 28000； 490000')
A283_TABLE_EVIDENCE = {
    'header': '序号 | 供应商名称 | 货物名称 | 货物品牌 | 货物型号 | 货物数量 | 货物单价(元)',
    'item_index': 1, 'item_count': 9, 'currency_context': '货物单价(元)'}


def _a283_model_reading():
    """模型通道那条：写出了品名，没有 ``table_evidence``。

    字段逐字取自 ``设计规范`` 与 a283 的重放结果（``task-a283e5adf675`` 的
    ``research-e623442a…`` + 判据修复单）：47000 那条是另一条公告，这里只放
    同一条公告上重复的 55000 两次读数。
    """
    return {'url': A283_NOTICE, 'model': 'DMi1', 'unit_price': 55000.0, 'amount': 55000,
            'quantity': 2, 'lot': '', 'item': '倒置显微镜', 'date': '2026-08-12', 'kind': 'award',
            'amount_scope': 'unit', 'currency': 'CNY', 'project_id': '09-02-04A-2026-D-F-E15648',
            'supplier': '北京睿辰生物医疗科技有限公司', 'excerpt': A283_EXCERPT}


def _a283_table_reading():
    """表格通道那条：金额、数量、供应商、型号都一样，品名/包号为空，多了表格证据。"""
    row = _a283_model_reading()
    row['lot'] = None
    row['item'] = None
    row['table_evidence'] = dict(A283_TABLE_EVIDENCE)
    return row


def test_the_table_reading_and_the_model_reading_are_one_transaction():
    model_row, table_row = _a283_model_reading(), _a283_table_reading()
    assert _same_transaction(model_row, table_row), '两次读数在双方都写明的字段上一致'
    assert len(_merge_price_rows([model_row, table_row])) == 1
    assert len(_merged_prices([model_row], [table_row])) == 1


def test_the_merged_row_is_the_union_of_both_readings():
    """用户要的「信息更全的那条」按并集算：品名来自模型，表格引文来自表格通道。"""
    merged = _merge_price_rows([_a283_model_reading(), _a283_table_reading()])[0]
    assert merged['item'] == '倒置显微镜'
    assert merged['unit_price'] == 55000.0
    assert merged['table_evidence']['item_index'] == 1
    assert merged['table_evidence']['currency_context'] == '货物单价(元)'
    # 反过来放也一样：谁先谁后都得到同一张并集。
    reversed_union = _merge_price_rows([_a283_table_reading(), _a283_model_reading()])[0]
    assert reversed_union['item'] == '倒置显微镜'
    assert reversed_union['table_evidence']['item_count'] == 9


def test_two_readings_that_disagree_on_the_quantity_stay_two_rows():
    """反例：数量是双方都写明的不同值，那就不是同一笔，不许合并。"""
    model_row, table_row = _a283_model_reading(), _a283_table_reading()
    table_row['quantity'] = 3
    assert not _same_transaction(model_row, table_row)
    assert len(_merge_price_rows([model_row, table_row])) == 2


def test_two_readings_that_disagree_on_the_amount_scope_stay_two_rows():
    """反例：一个是单价，一个是整包价——同一个数字也不是同一笔钱。"""
    model_row, table_row = _a283_model_reading(), _a283_table_reading()
    table_row['amount_scope'] = 'package_total'
    assert not _same_transaction(model_row, table_row)
    assert len(_merge_price_rows([model_row, table_row])) == 2


def test_the_same_model_at_two_prices_on_one_page_stays_two_rows():
    """反例（派单点名的第二条）：同 URL、同型号、不同单价的合法分项价。"""
    first = _a283_model_reading()
    second = _a283_model_reading()
    second['unit_price'] = 48000.0
    second['amount'] = 48000
    second['item'] = 'A2型生物安全柜'
    assert not _same_transaction(first, second)
    assert len(_merge_price_rows([first, second])) == 2
