"""迭代规范：``设计规范`` 独立验收的修复钉子（多型号页的行归属 + 闸门四条）。

一、多型号页"只取自己那一份"——三种结构各钉一条，都用真实页面上的形状：

* 型号在**列**上（``|`` 分隔）：表头 ``型号 | EAB 125i | … | EAB 224i``、数据行首格是参数名。
  实测 ``adamequipment.com`` 的 EAB 数据表（``b2-balance-ohaus`` 轮）：修前整张十型号对照表
  610 字进了 ``EAB 224i`` 的配置，原因是"像型号的一格"要求不含空格，``EAB 125i`` 数不出来，
  整页被判"不是宽表"；
* 型号在**行**上：表头 ``型号 | 量程(g) | …``、数据行首格是型号。实测 ``fzhz.com.cn`` 的
  PTY 系列（``b3-balance-mt-me204e-r1`` 轮）：修前五个型号的整表 420 字进了配置；
* **空格分隔**（PDF 手册抽取后的形状，没有 ``|``）：表头 ``ME204 ME204E``、数据行
  ``净重 4.7 kg 4.5 kg``。实测 ``mt.com`` 的 ``RM_ME_zh.pdf``（``设计规范`` §2a）。

二、判不出归属时**不许静默放行**：判据一个字没放宽（照旧采用），但这件事要同时出现在
``gaps``（给人看的话）与 ``failures``（给排查用的话）里。

三、闸门：没有配置就没有可转移的东西；``ok`` 路径也要如实覆盖留痕。
"""
from backend.services.procurement_research import (
    apply_configuration_model_gate,
    configuration_model_conflict,
    configuration_model_evidence,
    gate_candidate_configuration,
)
from backend.services.product_configuration import (
    _column_spec_text,
    _model_row_pairs,
    _space_column_pairs,
    model_scope_configuration,
    product_configuration,
)

#: 照抄真实页面形状（``adamequipment.com`` 的 EAB 数据表，``|`` 分隔、型号在列上）：
#: 型号写法**带空格**，正是修前"数不出像型号的一格"的那一处。
EAB_TABLE = '\n'.join([
    '型号 | EAB 125i | EAB 225i | EAB 124i | EAB 224i',
    '量程 | 62g / 120g | 82g / 220g | 120g | 220g',
    '精度 | 0.01mg / 0.1mg | 0.01mg / 0.1mg | 0.0001g | 0.0001g',
    '线性误差 | 0.08mg / 0.2mg | 0.1mg / 0.2mg | 0.0003g | 0.0003g',
])

#: 照抄 ``fzhz.com.cn`` 的 PTY 系列（``|`` 分隔、型号在行上）。
PTY_TABLE = '\n'.join([
    '型号 | 量程(g) | 可读性(mg) | 重复性(mg)',
    'PTY-124/223 | 120/220 | 0.1/0.5 | ±0.1/±0.5',
    'PTY-224/323 | 220/320 | 0.1/0.5 | ±0.1/±0.5',
    'PTY-324/423 | 320/420 | 0.1/0.5 | ±0.1/±0.5',
])

#: 照抄 ``mt.com`` 的 ``RM_ME_zh.pdf`` 10.2 节（**空格分隔**、型号在列上）。
MT_TABLE = '\n'.join([
    '10.2.1 可读性 0.1 mg 的天平（带防风罩）',
    'ME54 ME54E',
    '极限值',
    '最大秤量 52 g 52 g',
    '净重 4.7 kg 4.5 kg',
    '技术参数\u200b\u200b84 精密和分析天平',
    'ME204 ME204E',
    '极限值',
    '最大秤量 220 g 220 g',
    '可读性 0.1 mg 0.1 mg',
    '重复性（正常加载） 0.1 mg 0.1 mg',
    '稳定时间 2 s 2 s',
    '净重 4.7 kg 4.5 kg',
])


def source(body, url='https://example.com/spec.html', kind='product_detail'):
    return {'url': url, 'body_text': body, 'body_status': 'fetched',
            'source_type': kind, 'title': 'x'}


# ---------------------------------------------------------------- 一、三种结构

def test_model_in_columns_takes_only_the_target_column():
    """型号在列上（``EAB 224i``）：只取它那一列，别的列一个字都不进来。"""
    text, verdict = _column_spec_text(EAB_TABLE, 'EAB 224i')
    assert verdict == 'ok'
    assert '量程 | 220g' in text and '精度 | 0.0001g' in text
    assert '62g / 120g' not in text and '82g / 220g' not in text
    assert '0.01mg / 0.1mg' not in text


def test_model_in_columns_is_what_product_configuration_stores():
    """同一个形状在 ``product_configuration`` 里也走通（修前它会写整张表）。"""
    spec = product_configuration([source(EAB_TABLE)], 'EAB 224i', '奥豪斯')
    assert spec is not None
    assert spec['model_scope'] == 'model-column'
    assert '量程 | 220g' in spec['text']
    assert 'EAB 125i' not in spec['text'] and '62g / 120g' not in spec['text']


def test_model_in_rows_takes_only_the_target_row():
    """型号在行上（``PTY-224/323``）：只取它那一行。"""
    rows, reason = _model_row_pairs(PTY_TABLE, 'PTY-224/323')
    assert reason == 'ok'
    assert '量程(g) | 220/320' in rows
    assert '可读性(mg) | 0.1/0.5' in rows
    assert '120/220' not in rows and '320/420' not in rows
    spec = product_configuration([source(PTY_TABLE)], 'PTY-224/323', '华志')
    assert spec is not None and spec['model_scope'] == 'model-row'
    assert 'PTY-124/223' not in spec['text']


def test_space_separated_table_takes_only_the_target_column():
    """空格分隔的 PDF 手册表（``ME204E``）：只取它那一列（``4.5 kg`` 不是 ``4.7 kg``）。"""
    rows, reason = _space_column_pairs(MT_TABLE, 'ME204E')
    assert reason == 'ok'
    assert '净重 | 4.5 kg' in rows
    assert '最大秤量 | 220 g' in rows
    # ME54/ME54E 那一节（在 ME204 那一节之前）不属于这台机器：一行都不许进来
    assert '最大秤量 | 52 g' not in rows


def test_space_separated_table_never_takes_the_sibling_column():
    """同族型号（``ME204`` 与 ``ME204E``）分成两列：精确同一性优先，不取错列。"""
    rows, _reason = _space_column_pairs(MT_TABLE, 'ME204')
    assert '净重 | 4.7 kg' in rows
    assert '净重 | 4.5 kg' not in rows


# ---------------------------------------------------------------- 二、归属出口

def test_a_wide_table_whose_column_cannot_be_taken_is_left_empty():
    """认出了多型号表、却取不出目标那一份时**留空**，绝不退回整表。

    压缩那一遍仍然压得出东西（``量程 | 220g`` 带量），修前它就会成为这台机器的配置。
    """
    body = '\n'.join([
        '型号 | EAB 125i | EAB 224i',
        '量程 | 62g / 120g |',
        '精度 | 0.01mg / 0.1mg |',
        '量程 | 220g',
    ])
    assert product_configuration([source(body)], 'EAB 224i') is None


def test_the_column_proof_names_the_model_that_owns_the_configuration():
    """按列取出来的那一份，归属是**查出来的**（``model_scope``），不是猜的。"""
    spec = product_configuration([source(EAB_TABLE)], 'EAB 224i', '奥豪斯')
    candidate = {'model': 'EAB 224i', 'configuration': spec['text'],
                 'configuration_url': 'https://example.com/spec.html'}
    found, basis = configuration_model_evidence(candidate, [source(EAB_TABLE)])
    assert found == 'EAB 224i' and basis['rule'] == 'model_scope'
    assert model_scope_configuration(EAB_TABLE, 'EAB 224i')[0] == spec['text']


def test_an_unattributed_multi_model_page_is_written_into_gaps_and_failures():
    """归属判不出、而这一页并列着别的型号：判据不放宽，但 gaps 与 failures 各有一句话。"""
    page = '\n'.join([
        '型号 | PTY-124/223 | PTY-224/323 | PTY-324/423',
        '量程 | 120/220 | 220/320 | 320/420',
        '可读性 | 0.1/0.5 | 0.1/0.5 | 0.1/0.5',
    ])
    candidate = {'model': 'PTY-224/323', 'candidate_id': 'c1', 'configuration': page,
                 'configuration_source': 'product_page_spec',
                 'configuration_url': 'https://example.com/pty.html', 'source_pages': []}
    run = {'sources': [source(page, 'https://example.com/pty.html')],
           'alternatives': [candidate], 'gaps': [], 'failures': []}
    apply_configuration_model_gate(run, [candidate])
    # 判据没放宽：配置照旧采用（不是"判不出就当没有"）
    assert candidate['configuration'] == page
    assert configuration_model_conflict(candidate) is False
    assert candidate['configuration_model_gate']['action'] == 'ok'
    assert candidate['configuration_model_gate']['scope_undetermined'] is True
    assert candidate['configuration_model_basis']['multi_model'] is True
    assert 'PTY-124/223' in candidate['configuration_model_basis']['models']
    assert any('多型号对照表' in gap for gap in run['gaps'])
    codes = [row['code'] for row in run['failures']]
    assert codes == ['candidate_configuration_model_undetermined']


def test_a_single_model_page_gets_no_multi_model_note():
    """单型号页不会被当成"多型号对照表"，gaps 里不许出现那句话。"""
    page = '\n'.join(['XKA-2200 技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min',
                      '最大容量 | 400ml', '整机功率 | 1.65KW'])
    candidate = {'model': 'XKA-2200', 'candidate_id': 'c1', 'configuration': page,
                 'configuration_source': 'product_page_spec',
                 'configuration_url': 'https://example.com/xka.html', 'source_pages': []}
    run = {'sources': [source(page, 'https://example.com/xka.html')],
           'alternatives': [candidate], 'gaps': [], 'failures': []}
    apply_configuration_model_gate(run, [candidate])
    assert not candidate['configuration_model_gate'].get('scope_undetermined')
    assert not any('多型号对照表' in gap for gap in run['gaps'])
    assert run['failures'] == []


# ---------------------------------------------------------------- 三、闸门四条

def _run(candidates):
    return {'sources': [], 'alternatives': candidates, 'gaps': [], 'failures': []}


def test_an_empty_configuration_hands_nothing_over():
    """空配置 + 残留 ``configuration_model``：不许"凭空新建一条 0 字候选"。"""
    candidate = {'model': 'X-A', 'candidate_id': 'c1', 'configuration': '',
                 'configuration_url': '', 'configuration_model': 'Y-B', 'source_pages': []}
    run = _run([candidate])
    result = apply_configuration_model_gate(run, [candidate])
    assert result['dropped'] == [] and result['created'] == []
    assert run.get('reference_candidates') in (None, [])
    assert candidate['configuration_model_gate']['action'] == 'ok'
    assert 'configuration_model' not in candidate
    # "配置与出处都按原文照抄"这句话只在真有原文时才说
    assert not any('按原文照抄' in str(gap) for gap in run['gaps'])
    assert run['failures'] == []


#: 型号 + 中文商品后缀（``instrument.com.cn`` 的四型号对照表就是这么写的）；
#: 最后两行是同一页上**另一张表**（型号-报价清单，3 列，与上面 4 列不是一张表）。
SW_CJ_TABLE = '\n'.join([
    '型号 | SW-CJ-1F超净工作台 | SW-CJ-1FD超净工作台 | SW-CJ-2F超净工作台 | SW-CJ-2FD超净工作台',
    '洁净等级 | 100级 | 100级 | 100级 | 100级',
    '平均风速 | ≥0.3m/s | ≥0.3m/s | ≥0.3m/s | ≥0.3m/s',
    '电源 | AC 220V 50Hz | AC 220V 50Hz | AC 220V 50Hz | AC 380V 50Hz',
    '产品名称 | 型号 | 报价',
    '洁净工作台 | SW-CJ-1F/SW-CJ-1FD | 9800/9600',
])

#: 表里**没有目标型号那一列**（``m.yiqi.com/product/detail_1158100.html`` 的六型号表）。
NO_TARGET_COLUMN_TABLE = '\n'.join([
    '型号参数 | SW-CJ-1D | SW-CJ-2D | SW-CJ-1FD | SW-CJ-2FD | SW-CJ-3FD',
    '洁净等级 | 100级 | 100级 | 100级 | 100级 | 100级',
    '平均风速 | ≥0.3m/s | ≥0.3m/s | ≥0.3m/s | ≥0.3m/s | ≥0.3m/s',
])


def test_a_model_header_with_a_chinese_suffix_is_still_a_column():
    """表头写「型号 + 中文商品后缀」也要认（``设计规范`` §3.1）。

    上一版判据要求表头格"不含汉字"，于是 ``SW-CJ-1F超净工作台`` 数不出来、整页被判
    "不是宽表"退回压缩通路，把四型号整表 1207 字写成了 ``SW-CJ-1FD`` 的配置。
    """
    text, verdict = _column_spec_text(SW_CJ_TABLE, 'SW-CJ-1F')
    assert verdict == 'ok'
    assert '洁净等级 | 100级' in text
    # 数据行里的 ``AC 220V 50Hz`` 不是型号格：这一行不许被当成"又一行表头"跳过
    # （``设计规范`` §3.3：``JSC-V20A/B`` 就是这样丢掉本机的 ``电源`` 行的）
    assert '电源 | AC 220V 50Hz' in text


def test_a_row_from_another_table_is_not_taken():
    """同一页另一张表（型号-报价清单，列数不同）不许混进参数（``设计规范`` §3.1 的下一层）。"""
    text, _verdict = _column_spec_text(SW_CJ_TABLE, 'SW-CJ-1F')
    assert '产品名称' not in text and '报价' not in text


def test_a_multi_model_table_without_the_target_column_leaves_the_page_empty():
    """这一页有型号对照表、表里却没有目标型号那一列 → 整页留空（不许退回整表压缩）。"""
    text, verdict = _column_spec_text(NO_TARGET_COLUMN_TABLE, 'SW-CJ-1F')
    assert text == '' and verdict == 'no-target-column'
    body = 'SW-CJ-1F 产品页\n' + NO_TARGET_COLUMN_TABLE
    assert product_configuration([source(body)], 'SW-CJ-1F') is None


def test_the_density_denominator_is_the_window_not_the_column_text():
    """``_rank`` 的分母恒取清洗后的窗口（``设计规范`` §3.2）。

    拿按列取出来的短文本当分母会把密度放大、档内排序换尺子——59 轮口径下 4 条配置
    因此换了来源页（``LDZF-75KB-II`` 从厂家页 941 字换成聚合站 82 字）。
    """
    body = ('XA-100 产品页\n' + '导航栏\n' * 150
            + '型号 | XA-100 | XA-200\n量程 | 100g | 200g\n可读性 | 0.01mg | 0.02mg\n')
    spec = product_configuration([source(body)], 'XA-100')
    assert spec is not None and spec['model_scope'] == 'model-column'
    # 按列产出只有两行（几十字）；分母必须是窗口那个量级
    assert spec['rank_chars'] > 200
    assert len(spec['text']) < 60


def test_symbol_values_are_kept_verbatim():
    """打勾行的值（``●``／``―``）**逐字保留**，不翻成"有/无"，也不把别列的符号收回来。

    ``设计规范`` §7：``_looks_like_value('●')`` 原来是 False，``微电脑智能控制系统 | ●``
    这种本机功能行**整行丢了**。
    """
    body = '\n'.join([
        'LDZH-100KBS 立式压力蒸汽灭菌器',
        '型号 | LDZH-100KBS LDZH-150KBS LDZH-200KBS | LDZH-100L LDZH-150L LDZH-200L',
        '容积(L) | 100 | 200',
        '温度范围 | 50-134℃ | 50-134℃',
        '微电脑智能控制系统 | ● | ○',
        '安全联锁装置 | ● | ●',
        '倒计时预约功能 | ― | ―',
    ])
    spec = product_configuration([source(body)], 'LDZH-100KBS')
    assert spec is not None
    text = spec['text']
    assert '微电脑智能控制系统 | ●' in text
    assert '安全联锁装置 | ●' in text
    assert '倒计时预约功能 | ―' in text
    # 别列（``○``）不许收进来：取的是本机那一列
    assert '○' not in text


def test_a_header_whose_first_cell_is_not_the_word_model():
    """表头首格是「产品」这类短标签、列名带括号逗号，也要认（``yue17.com`` 那页）。"""
    body = '\n'.join([
        'ME204E 电子天平',
        '产品 | ***大载荷 | 分辨率 | 重复性',
        'ME204E | 220.0 g | 0.1 mg | 0.08 mg',
        'PTY-124/223 | 120 g | 0.1 mg | 0.1 mg',
    ])
    spec = product_configuration([source(body)], 'ME204E')
    assert spec is not None and spec['model_scope'] == 'model-row'
    assert '***大载荷 | 220.0 g' in spec['text']
    assert '120 g' not in spec['text']


def test_a_merged_model_column_is_expanded_by_common_prefix():
    """``SW-CJ-1F/1FD`` 这种**合并列名**先展开、再按同一性匹配，恰好一个才取这一列。"""
    body = '\n'.join([
        'SW-CJ-1F 净化工作台',
        '参数 | SW-CJ-1F/1FD | SW-CJ-2F/2FD',
        '洁净等级 | 100级 | 100级',
        '平均风速 | ≥0.3m/s | ≥0.3m/s',
    ])
    text, verdict = _column_spec_text(body, 'SW-CJ-1F')
    assert verdict == 'ok'
    assert '洁净等级 | 100级' in text
    # 展开出来的 ``SW-CJ-2F`` 不是 ``SW-CJ-1F``：目标不在这一页的表里就不取
    assert _column_spec_text(body, 'SW-CJ-9F')[1] != 'ok'


def test_a_page_whose_table_cannot_be_taken_leaves_a_note_in_gaps_and_failures():
    """「取不出就留空」必须有人话出口与排查出口（``设计规范`` §7 的第四条）。"""
    page = '\n'.join([
        'SW-CJ-1F 净化工作台',
        '型号参数 | SW-CJ-1D | SW-CJ-2D | SW-CJ-1FD | SW-CJ-2FD | SW-CJ-3FD',
        '洁净等级 | 100级 | 100级 | 100级 | 100级 | 100级',
    ])
    candidate = {'model': 'SW-CJ-1F', 'candidate_id': 'c1', 'configuration': '',
                 'detail_page': {'url': 'https://example.com/yiqi.html'},
                 'configuration_url': '', 'source_pages': []}
    run = {'sources': [source(page, 'https://example.com/yiqi.html')],
           'alternatives': [candidate], 'gaps': [], 'failures': []}
    apply_configuration_model_gate(run, [candidate])
    assert candidate['configuration_scope_gap']['verdict'] == 'leave-empty'
    assert candidate['configuration_scope_gap']['url'] == 'https://example.com/yiqi.html'
    assert any('没认出那张表' in gap for gap in run['gaps'])
    codes = [row['code'] for row in run['failures']]
    assert 'candidate_configuration_table_not_recognized' in codes


def test_the_gate_trace_follows_the_current_state():
    """回捞修好之后二次进闸门：留痕必须跟着状态走，不再写"已经转走"。"""
    wrong = '\n'.join(['型号 | Y-B', '量程 | 220g', '可读性 | 0.1mg'])
    page = {'url': 'https://example.com/y-b.html', 'body_status': 'fetched',
            'body_text': 'Y-B 产品页\n' + wrong, 'source_type': 'product_detail'}
    candidate = {'model': 'X-A', 'candidate_id': 'c1', 'configuration': wrong,
                 'configuration_source': 'product_page_spec', 'configuration_model': 'Y-B',
                 'configuration_url': 'https://example.com/y-b.html', 'source_pages': []}
    run = {'sources': [page], 'alternatives': [candidate], 'gaps': [], 'failures': []}
    apply_configuration_model_gate(run, [candidate])
    assert candidate['configuration_model_gate']['action'] == 'dropped'
    assert candidate['configuration_model_gate']['attribution'] == 'created'
    # 回捞把它修好了（换成自己那一页的配置），再跑一次闸门
    own = '\n'.join(['X-A 技术参数', '型号 | X-A', '量程 | 100g', '可读性 | 0.01mg'])
    own_page = {'url': 'https://example.com/x-a.html', 'body_status': 'fetched',
                'body_text': 'X-A 产品页\n' + own, 'source_type': 'product_detail'}
    run['sources'].append(own_page)
    candidate['configuration'] = own
    candidate['configuration_url'] = 'https://example.com/x-a.html'
    candidate.pop('configuration_model', None)
    candidate.pop('configuration_model_basis', None)
    outcome = gate_candidate_configuration(run, candidate)
    trace = candidate['configuration_model_gate']
    assert outcome['action'] == 'ok'
    assert trace['action'] == 'ok' and trace['attribution'] == ''
    assert trace['attributed_to'] == ''
    assert trace['previous_action'] == 'dropped'
    # 上一轮拦下的那个页面地址仍留着：回捞不再去试它
    assert trace['rejected_url'] == 'https://example.com/y-b.html'
