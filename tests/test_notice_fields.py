"""来源级公告结构字段：能抽到什么、抽不到怎么办、逐包供应商的边界在哪。

根因（``设计说明``）：``price_regions`` 只切型号行上下
三行，实测窗口 37 个字符，而抽取 prompt 要求模型返回 ``project_id`` 与
``procurement_unit``——窗口里根本没有这些字段，模型只能返回空串，前端于是永远
显示空白。这里钉住「在来源级读一次正文」这条补法，以及它**不许**越过的边界。
"""
from pathlib import Path

from backend.services.procurement_tables import price_table_rows
from backend.services.research_sources import (
    _is_header_word, apply_notice_fields, lot_suppliers, notice_fields, price_regions,
    supplier_for)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FIXTURES = Path(__file__).parent / 'fixtures' / 'notice-fields'

#: 逐字段换行的真实公告：采购单位在第 22/29 行、项目编号在第 46 行，型号在第 107 行。
SPLIT_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
#: CCGP 把标签和值分成两行的公告：``一、项目编号`` 独占一行，值在下一行。
LABELLED_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202512/t20251209_25889616.htm'
#: 采购人只写在 ``1.采购人信息`` 小节里的公告，且第三个同类小节是监管部门。
SECTION_URL = 'http://ztb.usx.edu.cn/info/1022/15519.htm'
#: 一手明细表带供应商列的那条公告（上一轮真跑取到 150000 元的就是它）。
HOT_URL = 'http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/t20260812_27118698.htm'

XKA_EXCERPT = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'


def notice(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def test_the_window_that_missed_the_fields_is_still_the_window():
    """先钉住失败形状本身：37 个字符的窗口里确实一个结构字段都没有。"""
    body = notice('ccgp-split-field-notice.txt')
    regions = price_regions(body, 'XKA-2200')
    assert len(regions) == 1
    assert len(regions[0]['text']) == 37
    for field in ('项目编号', '采购单位', '采购人', '代理机构', '成交供应商'):
        assert field not in regions[0]['text']


def test_split_field_notice_yields_the_header_fields():
    """型号行离头部 60 行以外，仍能从同一份正文抽到项目编号与采购单位。"""
    fields = notice_fields(notice('ccgp-split-field-notice.txt'))
    assert fields['project_id'] == 'YXZC2026-G1-00082-HCZB-0013'
    assert fields['procurement_unit'] == '峨山彝族自治县医疗与健康服务集团'
    assert fields['agency'] == '云南鸿诚项目管理咨询有限公司'
    assert fields['notice_date'] == '2026-03-25'


def test_label_on_its_own_line_reads_the_value_below():
    """``一、项目编号`` 与 ``FS34000120257981号001`` 是两行，值在下一行。"""
    fields = notice_fields(notice('ccgp-labelled-line-notice.txt'))
    assert fields['project_id'] == 'FS34000120257981号001'
    assert fields['procurement_unit'] == '合肥大学'


def test_purchaser_section_never_borrows_the_supervisor():
    """``3.同级政府采购监督管理部门`` 的 ``名 称：绍兴市财政局`` 不是采购人也不是代理机构。"""
    fields = notice_fields(notice('usx-purchaser-section-notice.txt'))
    assert fields['procurement_unit'] == '绍兴文理学院'
    assert fields['agency'] == '浙江建诚工程管理咨询有限公司'
    assert '财政' not in fields['agency']
    assert fields['procurement_unit'] != '绍兴市财政局'


def test_a_header_row_is_not_a_supplier_value():
    """``序号 | 供应商名称 | 供应商地址 | 金额`` 是表头，不能读出「供应商地址」。"""
    lots = lot_suppliers(notice('ccgp-labelled-line-notice.txt'))
    assert '供应商地址' not in lots.values()


def test_the_sibling_column_name_is_not_a_supplier_value():
    """真实缺陷（大连公共资源交易网 DLZY-2025-0431 大连大学项目中标结果公告）。

    第 19 行 ``序号 | 中标（成交）金额(元) | 中标供应商名称 | 中标供应商地址 | 推荐理由``：
    ``中标供应商名称`` 精确命中标签，紧跟的兄弟列名 ``中标供应商地址`` 被当成值返回，
    ``lot_suppliers`` 于是给出 ``{'': '中标供应商地址'}``——列名被当成供应商，而且会
    经 ``apply_notice_fields`` 真的落库。第 22 行
    ``标项号 | 供应商名称 | 资格审查结果 | 总得分 | 排名 | 未通过资格审查原因``
    同理会抽出 ``资格审查结果``。
    """
    body = notice('ggzyjy-dl-award-notice.txt')
    assert '中标供应商名称 | 中标供应商地址' in body
    assert '供应商名称 | 资格审查结果' in body
    assert lot_suppliers(body) == {}
    assert '中标供应商地址' not in lot_suppliers(body).values()


def test_the_column_name_predicate_is_a_tail_rule_not_a_longer_word_list():
    """判据换成了「短词以真实列名词尾结尾」，不是往词表里补这两个词。

    兄弟列名是开放集合：``_HEADER_NEIGHBOURS`` 里有 ``供应商地址``，但补不进
    ``中标供应商地址``、``资格审查结果``、``未通过资格审查原因`` 这些带前缀的写法。
    """
    for column in ('中标供应商地址', '资格审查结果', '未通过资格审查原因', '评审总得分',
                   '中标（成交）金额(元)', '单价（元）', '报价(总价，元)', '代理机构联系方式',
                   '产 地', '单 位'):
        assert _is_header_word(column) is True, column


def test_real_supplier_and_lot_names_are_not_column_names():
    """反向验证：实测抽对的真实值一个都不许被当列名拒掉。

    前三个来自云南那条逐字段换行的公告（``test_each_lot_keeps_its_own_supplier``），
    中间两个来自剑鱼真实公告，最后一个短的 ``第1包``／``第一标段`` 是标包名——
    词尾表里故意没有 ``包``／``标段``／``标项``，就是为了它们。
    """
    for value in ('卓越涵宇国际贸易（辽宁）有限公司', '云南和昂医疗器械有限责任公司',
                  '云南速霖商贸有限公司', '惠而好医疗科技（云南）有限公司',
                  '河南缔恩地美仪器设备有限公司', '山西凯力晟达科技有限公司',
                  '大连中远招标代理有限公司', '公诚管理咨询有限公司',
                  '第1包', '第一标段', '标的1', '1'):
        assert _is_header_word(value) is False, value


#: 22 个真实列名 / 36 个真实值，逐条抄自
#: ``output/pipe-supervisor-20260923/site-probe/probe_A_headerwords.py``（主管道独立验证
#: 用的那张表，数据来自大连公告与真实 run）。固化成断言表，是因为**误杀比漏判危险**：
#: 以后谁再动 ``_COLUMN_NAME_TAILS``，这里会立刻红。
COLUMN_NAMES = (
    '中标供应商名称', '中标供应商地址', '资格审查结果', '未通过资格审查原因',
    '供应商名称', '供应商地址', '总得分', '排名', '推荐理由', '单价（元）',
    '中标（成交）金额(元)', '序号', '标项号', '报价(总价，元)', '规格型号',
    '品牌', '数量', '单价', '备注', '其他事项', '标的名称', '废标理由',
)
#: 判为列名就会把真数据丢掉的那些值。含两类最容易误伤：真实供应商名、
#: 以及 ``第1包``／``第一标段``／``包别1``／``合同包1``／``标项1`` 这五个标包名。
CELL_VALUES = (
    '卓越涵宇国际贸易（辽宁）有限公司', '大连东润林科技有限公司', '大连百特科技有限公司',
    '大连佳创恒隆科技有限公司', '云南和昂医疗器械有限责任公司', '云南速霖商贸有限公司',
    '惠而好医疗科技（云南）有限公司', '湖南缔恩地美仪器设备有限公司',
    '山西凯力晟达科技有限公司', '大连中远招标代理有限公司',
    '辽宁省大连市沙河口区星文街12号', '大连市经济技术开发区学府大街10号',
    '报价：501978（元）', '95.48', '91.2', '1', '-', '通过', '加热磁力搅拌器',
    '艾卡', 'IKA RCT basic', '11800',
    '王琦，李梦轲，丁彦闯，吴志坚，王爱玲（第1标项名称采购人代表）',
    '大连大学', '大连大学2025年环化学院教学实验室建设项目', '2025-06-09',
    '0411-87403597', '滕惠宇', '按招标文件要求', '7530', '自本公告发布之日起1个工作日。',
    '第1包', '第一标段', '包别1', '合同包1', '标项1',
)


def test_the_real_column_names_and_values_table():
    """22 个真实列名全认得出，36 个真实值一个都不许被误伤。"""
    missed = [cell for cell in COLUMN_NAMES if not _is_header_word(cell)]
    killed = [cell for cell in CELL_VALUES if _is_header_word(cell)]
    assert missed == []
    assert killed == []


def test_a_short_address_ending_in_号_is_still_a_value():
    """``号`` 不能进词尾表：``地 址 | 学府大街10号`` 这种行里它是真值。

    大连那条公告第 46 行就是 ``地 址： 大连市经济技术开发区学府大街10号``
    （实测值 12 字，本来就超上限），而 ``学府大街10号`` 只有 6 个字——一旦 ``号``
    成为合法词尾，它就会被 ``_value_cell`` 当成兄弟列名丢掉。
    """
    assert _is_header_word('学府大街10号') is False
    assert _is_header_word('标项号') is True


def test_the_real_dalian_notice_still_yields_its_header_fields():
    """同一份真实正文里的公告级字段不受影响：编号、采购人、代理机构照旧抽得到。"""
    fields = notice_fields(notice('ggzyjy-dl-award-notice.txt'))
    assert fields['project_id'] == 'DLZY-2025-0431'
    assert fields['procurement_unit'] == '大连大学'
    assert fields['agency'] == '大连中远招标代理有限公司'


def test_a_label_and_its_value_on_one_line_are_not_a_header_row():
    """行级判据不能只数列名个数。

    剑鱼那条郑州大学的真实公告里有
    ``中标单位 | 河南缔恩地美仪器设备有限公司 | 中标金额 | ***``：两个列名词中间
    夹着一个真供应商名。只按「一行里 ≥2 个列名就是表头」判，这一行会被整行丢掉，
    实测抽对的供应商名跟着丢。所以行级判据还要求「每个列名后面跟的还是列名」——
    ``采购人 | 大连大学 | 代理机构 | 某公司`` 这种一列标签一列值的写法同样靠这一条保命。
    """
    body = notice('jianyu-henan-supplier.txt')
    assert '中标单位 | 河南缔恩地美仪器设备有限公司 | 中标金额 | ***' in body
    assert lot_suppliers(body) == {'': '河南缔恩地美仪器设备有限公司'}


def test_the_real_supplier_header_row_yields_nothing():
    """山西沁水那条：``序号 | 供应商名称 | 供应商地址 | 中标（成交）金额 | 评审总得分``。

    这里的供应商名在下一行，而标签后面紧跟的是兄弟列名，所以一个值都不该取。
    """
    body = notice('jianyu-shanxi-supplier-header.txt')
    assert '供应商名称 | 供应商地址' in body
    assert lot_suppliers(body) == {}
    assert '供应商地址' not in lot_suppliers(body).values()


def test_the_expert_score_table_header_is_a_header_row():
    """温州医科大学那份真实公告（gzc.wmu.edu.cn）里的评分明细表头：

    ``标项 | 供应商名称 | 专家1 | 专家2 | 专家3 | 专家4 | 专家5 | 商务技术得分 | 报价得分 | 总分``

    ``专家1`` 到 ``专家5`` 是政采云模板生成的列头。不认它们，「供应商名称」后面那格就
    被当成值，``lot_suppliers`` 会给出 ``{'': '专家1'}``——旧代码给的是
    ``{'': '中标供应商地址'}``（同一份公告第 71 行的中标结果表）。
    真供应商名 ``杭州合众生物科技有限公司`` 在下一行的数据行里，那一行没有标签词，
    两版都抽不到（表格得按列位置解析，不在这个匹配器的职责里）。
    """
    body = notice('wmu-expert-score-table.txt')
    assert '供应商名称 | 专家1 | 专家2' in body
    assert '杭州合众生物科技有限公司' in body
    assert lot_suppliers(body) == {}


def test_an_expert_column_header_is_not_any_word_plus_a_digit():
    """``专家1`` 是列名，``标项1``／``第1包`` 是标包名（值）。

    数字结尾不能一概当列名——那正是词尾表不收 ``标项``／``包`` 的同一个理由。
    """
    for column in ('专家1', '专家', '专家 12'):
        assert _is_header_word(column) is True, column
    for value in ('标项1', '标项 1', '第1包', '包1', '1'):
        assert _is_header_word(value) is False, value


def test_each_lot_keeps_its_own_supplier():
    """三个标包三家供应商；XKA-2200 在第 4 包，只能取第 4 包那一家的。"""
    body = notice('ccgp-split-field-notice.txt')
    lots = lot_suppliers(body)
    assert lots == {'1': '云南和昂医疗器械有限责任公司',
                    '3': '云南速霖商贸有限公司',
                    '4': '惠而好医疗科技（云南）有限公司'}
    assert supplier_for(body, XKA_EXCERPT, lots) == '惠而好医疗科技（云南）有限公司'
    assert supplier_for(body, XKA_EXCERPT, lots) != lots['1']


def test_a_trailing_note_is_not_part_of_the_project_id():
    """真实原文第 38 行：``项目编号：09-02-04A-… （招标文件编号：09-02-04A-…）``。

    招标文件编号是另一件事，不能跟着项目编号一起进 ``project_id``。
    """
    body = ('一、项目编号：09-02-04A-2026-D-F-E15648 '
            '（招标文件编号：09-02-04A-2026-D-F-E15648）\n')
    assert notice_fields(body)['project_id'] == '09-02-04A-2026-D-F-E15648'


def test_a_parenthesised_company_name_keeps_its_brackets():
    """同一个清洗规则**不能**用到采购单位上：括号就是公司名的一部分。"""
    body = '采购单位 | 惠而好医疗科技（云南）有限公司\n'
    assert notice_fields(body)['procurement_unit'] == '惠而好医疗科技（云南）有限公司'


def test_multi_lot_notice_gives_no_supplier_when_the_price_cannot_be_placed():
    """分不清属于哪个标包时留空，绝不套公告里别的标包的那一家。"""
    body = notice('ccgp-split-field-notice.txt')
    lots = lot_suppliers(body)
    assert supplier_for(body, '这段引文不在正文里', lots) == ''
    assert supplier_for(body, '', lots) == ''


def test_single_lot_notice_falls_back_to_its_only_supplier():
    body = ('某某医院设备采购中标公告\n项目编号：ABC-2026-001\n采购人：某某医院\n'
            '供应商名称：某某医疗器械有限公司\n单价(元)：1200\n')
    lots = lot_suppliers(body)
    assert lots == {'': '某某医疗器械有限公司'}
    assert supplier_for(body, '单价(元)：1200', lots) == '某某医疗器械有限公司'


def test_fields_are_injected_into_every_price_of_the_same_source():
    body = notice('ccgp-split-field-notice.txt')
    fields, lots = notice_fields(body), lot_suppliers(body)
    record = {'excerpt': XKA_EXCERPT, 'gaps': []}
    apply_notice_fields(record, fields, lots, body)
    assert record['project_id'] == 'YXZC2026-G1-00082-HCZB-0013'
    assert record['procurement_unit'] == '峨山彝族自治县医疗与健康服务集团'
    assert record['agency'] == '云南鸿诚项目管理咨询有限公司'
    assert record['notice_date'] == '2026-03-25'
    assert record['supplier'] == '惠而好医疗科技（云南）有限公司'
    assert record['gaps'] == []


def test_absent_fields_stay_absent_and_are_named_in_gaps():
    """抽不到就留空并如实记缺口，不拿别的公告或品类常识补。"""
    record = {'excerpt': '单价(元)：1200', 'gaps': []}
    apply_notice_fields(record, {}, {}, '一份没有任何结构字段标签的正文')
    assert 'project_id' not in record
    assert not record.get('supplier')
    for key in ('project_id', 'procurement_unit', 'agency', 'supplier'):
        assert key in record['gaps']


# ---------------------------------------------------------------------------
# 端到端：价格链里注入的是什么
# ---------------------------------------------------------------------------

def _source(url, body):
    return {'url': url, 'body_status': 'fetched', 'source_type': 'tender_document',
            'body_text': body}


def _extractor(service, extraction=None):
    """装一个只回一条价格的抽取客户端；调用次数靠它自己数。"""
    calls = []

    class Client:
        endpoint = 'test://notice-fields'
        model = 'offline'
        last_usage = None

        def analyze(self, **kwargs):
            calls.append(kwargs.get('user_prompt', ''))
            return {'prices': [] if extraction is None else [dict(extraction)]}

    service.client_factory = Client
    return calls


def _extraction(**overrides):
    """模型在 37 个字符的窗口里能给出的东西：金额与型号，结构字段全是空串。"""
    row = {'brand': '湘仪', 'model': 'XKA-2200', 'procurement_unit': '', 'configuration': 'unknown',
           'kind': 'award', 'amount': 17480, 'amount_unit': '元', 'amount_scope': 'unit',
           'quantity': 1, 'currency': 'CNY', 'date': '2026-03-25', 'project_id': '',
           'lot': '', 'item': '', 'tax': 'unknown', 'installation': 'unknown',
           'warranty': 'unknown', 'shipping': 'unknown', 'service_response': 'unknown',
           'service_onsite': 'unknown', 'delivery': 'unknown', 'excerpt': XKA_EXCERPT}
    row.update(overrides)
    return row


def test_the_price_lane_fills_the_header_fields_the_window_never_showed(research):  # noqa: F811
    """端到端：抽取模型只拿到 37 个字符的窗口，结构字段只能是空串——来源级补上。

    改之前这条价格到达前端时 ``project_id`` 与 ``procurement_unit`` 都是空的，
    前端 ``ResearchResults.jsx`` 照渲染，于是页面上一片空白。
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_source(SPLIT_URL, notice('ccgp-split-field-notice.txt'))]
    calls = _extractor(service, _extraction())

    prices = service.prices(run, {'brand': '湘仪', 'model': 'XKA-2200'})

    assert len(calls) == 1
    assert len(prices) == 1
    price = prices[0]
    assert price['unit_price'] == 17480          # 判据没变，钱还是原来那条
    assert price['project_id'] == 'YXZC2026-G1-00082-HCZB-0013'
    assert price['procurement_unit'] == '峨山彝族自治县医疗与健康服务集团'
    assert price['agency'] == '云南鸿诚项目管理咨询有限公司'
    assert price['supplier'] == '惠而好医疗科技（云南）有限公司'
    assert 'project_id' not in price['gaps']


def test_the_source_level_value_beats_the_rows_own_guess(research):  # noqa: F811
    """模型从窗口里猜出来的编号/单位不让它留下——公告头条上的标签才是这份公告的。

    一份公告的每条价格各猜一次，正是要避免的东西：猜错的那些会互相矛盾。
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_source(SPLIT_URL, notice('ccgp-split-field-notice.txt'))]
    _extractor(service, _extraction(project_id='项目名称的一半',
                                    procurement_unit='某处猜的单位'))

    price = service.prices(run, {'brand': '湘仪', 'model': 'XKA-2200'})[0]

    assert price['project_id'] == 'YXZC2026-G1-00082-HCZB-0013'
    assert price['procurement_unit'] == '峨山彝族自治县医疗与健康服务集团'


# ---------------------------------------------------------------------------
# 表格通道自己也要产出这些字段（一手明细表带供应商列时）
# ---------------------------------------------------------------------------

def test_the_notice_table_channel_reads_its_own_supplier_column():
    """真实表头 ``序号 | 供应商名称 | … | 货物单价(元)``，供应商只在这一行里。

    这条 150000 元（海尔 DW-86L578S，第 3 包第 3 个型号）在改之前只有金额和型号，
    前端那个"供应商"位置永远不会有值。
    """
    rows = price_table_rows(notice('ccgp-supplier-column-table.txt'), 'DW-86L578S')
    assert len(rows) == 1
    assert rows[0]['amount'] == 150000.0
    assert rows[0]['supplier'] == '北京兰博泰斯生物科技有限公司'
    assert rows[0]['table_evidence']['item_index'] == 2
    assert rows[0]['table_evidence']['item_count'] == 27


def test_a_merged_supplier_header_is_not_read_as_a_supplier():
    """合并单元格展开后，数据格里写的还是列名——那不是供应商。"""
    body = ('序号 | 供应商名称 | 货物型号 | 货物单价(元)\n'
            '1 | 中标供应商 | DW-86L578S | 150000\n')
    rows = price_table_rows(body, 'DW-86L578S')
    assert len(rows) == 1
    assert 'supplier' not in rows[0]


def test_the_table_channel_reads_a_purchaser_column_when_the_table_has_one():
    body = ('序号 | 货物型号 | 采购单位 | 供应商名称 | 货物单价(元)\n'
            '1 | DW-86L578S | 峨山彝族自治县医疗与健康服务集团 | 云南和昂医疗器械有限责任公司 | 150000\n')
    rows = price_table_rows(body, 'DW-86L578S')
    assert len(rows) == 1
    assert rows[0]['procurement_unit'] == '峨山彝族自治县医疗与健康服务集团'
    assert rows[0]['supplier'] == '云南和昂医疗器械有限责任公司'


def test_the_table_channel_does_not_invent_columns_it_never_had():
    """表里没有的身份列不写键——空串会让"本来没有"和"没读到"分不开。"""
    body = ('序号 | 货物型号 | 货物数量 | 货物单价(元)\n'
            '1 | DW-86L578S | 1 | 150000\n')
    rows = price_table_rows(body, 'DW-86L578S')
    assert len(rows) == 1
    for key in ('supplier', 'procurement_unit', 'project_id'):
        assert key not in rows[0]


def test_the_price_lane_carries_both_the_row_supplier_and_the_notice_header(research):  # noqa: F811
    """端到端：表格通道给出这一行的供应商，来源级给出公告头的编号与采购单位。

    这两件事必须同时成立，否则三条通道的字段仍然不齐——最权威的一手表格曾经是
    字段最少的那个。
    """
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [_source(HOT_URL, notice('ccgp-hot-notice-full.txt'))]
    _extractor(service)

    prices = service.prices(run, {'brand': '海尔', 'model': 'DW-86L578S'})

    assert len(prices) == 1
    price = prices[0]
    assert price['amount'] == 150000.0
    assert price['supplier'] == '北京兰博泰斯生物科技有限公司'
    assert price['project_id'] == '09-02-04A-2026-D-F-E15648'
    assert price['procurement_unit'] == '中国疾病预防控制中心'
    assert price['agency'] == '公诚管理咨询有限公司'

