"""迭代规范：配置所属型号 + 撤除前回捞（设计规范的两条口径）。

依据 ``设计规范``。真实案例都在洁净台那一轮（``b3-opt-cleanbench-boxun-bj1cd``）：

* ``bekomat30``（品牌"济南欧莱博科学仪器有限公司"）的卡片链接指向
  ``olaibo.com/ProductDetail/634.html``（那页型号是 ``BBS-H1300``），抽到的配置却来自
  ``olaibo.com/ProductDetail/630.html``（那页是 ``BBS-V600``，配置第一行就写着），而
  ``bekomat30`` 两个页面上都不存在。设计规范：**配置属于哪个型号就展示哪个型号**。
* 贝茵 ``BCV-3S1`` 被判"没有配置"撤掉，可同一轮的来源里 ``jinpanmed.com``（219 字）、
  ``beinglab.com``（192 字）、``medwant.com``（98 字）三页都能压出它的配置——当时只试了
  错的那一页（成交公告，0 字）。这是"没试完就被当成没有"。

本文件钉住的是**判据与落点**：认不出就留空、一页多型号不许取第一个、部件型号不算、
回捞只读本轮已有来源且不改判据。
"""
from backend.services.procurement_research import (
    candidate_has_configuration, configuration_model_from_configuration,
    configuration_model_from_page, model_tokens_in_line, model_tokens_in_text,
    record_configuration_model, refetch_configuration_from_sources,
    withdraw_unconfigured_candidates,
)

#: 630 页那一段配置的真实开头（``型号 技术参数 | BBS-V600``）。
BBS_CONFIGURATION = ('型号 技术参数 | BBS-V600\n外形尺寸 | 600×580×1060mm\n'
                     '操作区尺寸 | 580×520×610mm\n额定电压 | 220V±10% 110V±10％\n'
                     '气流流速 | 0.3-0.5m/s\n过滤效率 | 99.999％\n')

#: 贝茵那段 219 字配置的真实形状（``型号技术参数：BCV-3S1``）。
BCV_CONFIGURATION = ('技术参数：\n型号技术参数：BCV-3S1\n适用人数：单人单面\n'
                     '气流流向：垂直流\n洁净等级：ISO 5 级\n平均风速：≥0.3m/s\n'
                     '噪音：≤62dB\n电源：AC220V/50Hz\n最大功率：250VA\n照度：≥900Lx\n')

BCV_URL = 'https://www.jinpanmed.com/archives/52781'
OLAIBO_630 = 'https://www.olaibo.com/ProductDetail/630.html'


def _page(url, body, source_type='manufacturer_website'):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': source_type, 'body_text': body}


# --------------------------------------------------- 一、配置文本自己写着的型号

def test_the_model_line_of_the_configuration_names_the_model():
    """洁净台那条：配置第一行 ``型号 技术参数 | BBS-V600`` → 这份配置属于 BBS-V600。"""
    assert configuration_model_from_configuration(BBS_CONFIGURATION) == (
        'BBS-V600', '型号 技术参数 | BBS-V600')


def test_the_chinese_colon_line_is_recognised_too():
    assert configuration_model_from_configuration(BCV_CONFIGURATION) == (
        'BCV-3S1', '型号技术参数：BCV-3S1')


def test_a_component_model_is_not_the_configurations_model():
    """634 页那行「风机型号：泛仕达FH320A风机」是**风机**的型号，不是这台机器的。

    行首不是「型号」的一律不算——把部件型号挂到整机上，比留空更糟。
    """
    configuration = '型号：| BBS-H1300\n风机型号：泛仕达FH320A风机，转速:1390 RPM\n'
    model, excerpt = configuration_model_from_configuration(configuration)
    assert model == 'BBS-H1300'
    assert '风机' not in excerpt


def test_a_model_line_with_several_models_is_not_guessed():
    """一页多型号：不取第一个，留空并说明为什么。"""
    model, reason = configuration_model_from_configuration('型号：BCV-3S1 / BCV-3S2\n')

    assert model is None
    assert '多个型号' in reason


def test_parameter_values_that_look_like_models_are_not_models():
    """参数段里到处是 ``AC220V``／``250VA``／``900Lx`` 这种"字母+数字"的形状。

    BCV 那段真实配置里就有这三个。**不在整段参数里捞型号**：试过一版"整段恰好一个型号
    就用它"，结果 ``AC220V`` 被当成了型号。只有行首写着"型号"的那一行算数，没有就留空
    交给页面那一层。
    """
    assert configuration_model_from_configuration(BCV_CONFIGURATION)[0] == 'BCV-3S1'
    only_values = ('技术参数：\n适用人数：单人单面\n洁净等级：ISO 5 级\n'
                   '电源：AC220V/50Hz\n最大功率：250VA\n照度：≥900Lx\n')
    model, reason = configuration_model_from_configuration(only_values)
    assert model is None
    assert '型号' in reason


def test_a_model_named_mid_line_without_a_label_is_left_to_the_page_layer():
    """没有"型号"标签行时不猜：交给页面那一层的同行／同列判法。"""
    configuration = '主要参数\n适用人数：单人单面\n设备：ABC-100 洁净工作台\n'
    model, reason = configuration_model_from_configuration(configuration)

    assert model is None
    assert '型号' in reason


# --------------------------------------------------- 一之二、型号 token 不在点号处断开

def test_a_dotted_model_is_not_cut_at_the_dot():
    """新华 ``LMQ.C-80E``：型号里的点号不该把 token 截断（实测那条被切成了 ``C-80E``）。

    扫描用的字符集与价格那把尺子同一个形状，只多一个点（``_MODEL_TOKEN_WITH_DOT``）；
    判据仍是 ``_is_plausible_model``，没有放宽。
    """
    assert model_tokens_in_text('型号： LMQ.C-80E') == ['LMQ.C-80E']
    assert configuration_model_from_configuration('型号： LMQ.C-80E\n设计温度 | 150℃\n') == (
        'LMQ.C-80E', '型号： LMQ.C-80E')


def test_the_dotted_model_reaches_the_candidate():
    candidate = {'model': 'LMQ.C-80E', 'configuration': '型号： LMQ.C-80E\n设计温度 | 150℃\n',
                 'configuration_url': 'https://shop.test/lmq'}

    assert record_configuration_model(
        candidate, [_page('https://shop.test/lmq', '型号： LMQ.C-80E\n')]) is True
    assert candidate['configuration_model'] == 'LMQ.C-80E'


def test_a_sentence_final_dot_and_a_separator_are_still_cuts():
    """只在"点号夹在两段型号字符之间"时把它吸进 token：句末的点、``|``／中文照旧断开。"""
    assert model_tokens_in_text('型号：BKQ-B75II. 容积：75L') == ['BKQ-B75II']
    assert model_tokens_in_text('型号：BKQ-B75II|容积：75L') == ['BKQ-B75II']
    assert model_tokens_in_text('型号：BKQ-B75II容积：75L') == ['BKQ-B75II']


def test_a_dotted_unit_value_is_not_a_model():
    """``0.5-1.5m/s`` 这类带点的参数值不许因为"点号不断开"就变成型号。"""
    assert model_tokens_in_text('气流流速 | 0.5-1.5m/s', strict=True) == []


# --------------------------------------------------- 一之三、一行多个型号时取与候选同一个的

def test_a_model_line_listing_several_models_takes_the_one_that_is_the_candidate():
    """一行写着三个型号（申安 ``LDZH-100KBS,LDZH-150KBS,LDZH-200KBS``）：取本候选那个。

    旧代码在这一行上直接判"多个型号"留空（实测 41 条空值里有 35 条是这一路）。
    """
    line = '型号：LDZH-100KBS,LDZH-150KBS,LDZH-200KBS,非医用型：LDZH-100L\n'

    assert configuration_model_from_configuration(line, 'LDZH-150KBS') == (
        'LDZH-150KBS', '型号：LDZH-100KBS,LDZH-150KBS,LDZH-200KBS,非医用型：LDZH-100L')
    model, reason = configuration_model_from_configuration(line, 'LDZH-999KBS')
    assert model is None
    assert '都不是本候选' in reason


def test_the_candidate_chinese_suffix_still_matches_the_page_spelling():
    """判"是同一个"用 ``identity`` ＋ ``model_spellings``：候选 ``BXM-30R双列六孔`` 认 ``BXM-30R``。"""
    page = '型号 | BXM-30R | BXM-50R\n容积 | 30L | 30L\n'
    model, excerpt = configuration_model_from_page(page, '容积 | 30L\n', 'BXM-30R双列六孔')

    assert model == 'BXM-30R'
    assert excerpt == '型号 | BXM-30R | BXM-50R'


def test_the_same_model_written_twice_on_one_line_still_resolves():
    """同一个型号在一行里写两遍（``BKQ-B75II``／``bkq-b75ii``）：取它自己。

    "匹配到多个型号与本候选是同一个"眼下不会发生——扫描时已经按 ``identity`` 去重
    （两个写法归一后是同一个 token），所以这一行仍然只认出一个型号。
    """
    model, excerpt = configuration_model_from_configuration(
        '型号：BKQ-B75II / bkq-b75ii\n', 'BKQ-B75II')

    assert model == 'BKQ-B75II'
    assert excerpt == '型号：BKQ-B75II / bkq-b75ii'


# --------------------------------------------------- 一之四、罗马数字后缀不在中间截断

def test_a_roman_numeral_suffix_is_not_cut_off():
    """博迅 ``BXM-170V-MⅡ``：``Ⅱ`` 不在旧字符集里，被截成 ``BXM-170V-M``（8 条台账都是它）。"""
    assert model_tokens_in_text('型号：BXM-170V-MⅡ') == ['BXM-170V-MⅡ']
    assert configuration_model_from_configuration('型号：BXM-170V-MⅡ\n容积 | 5L\n', 'BXM-170V-MⅡ') == (
        'BXM-170V-MⅡ', '型号：BXM-170V-MⅡ')


def test_the_roman_numeral_and_its_ascii_spelling_are_the_same():
    """``Ⅱ`` 与 ``II`` 归一后是同一个（``identity`` 做 NFKC），页面写哪种都认。"""
    assert configuration_model_from_configuration('型号：BXM-170V-MII\n', 'BXM-170V-MⅡ')[0] \
        == 'BXM-170V-MII'
    assert configuration_model_from_configuration('型号：BXM-170V-MⅡ\n', 'BXM-170V-MII')[0] \
        == 'BXM-170V-MⅡ'


def test_a_middle_roman_numeral_is_kept_too():
    """罗马数字在型号中间（``DK-98-ⅡA``）也不许断开。"""
    assert model_tokens_in_text('型号：DK-98-ⅡA', strict=True) == ['DK-98-ⅡA']


def test_a_type_suffix_is_the_same_machine_in_both_directions():
    """``YXQ-50G型`` 的「型」是写法差异：两个方向都记**候选自己的**写法，不显示成两台机器。"""
    assert configuration_model_from_configuration('型号：YXQ-50G型\n', 'YXQ-50G')[0] == 'YXQ-50G'
    assert configuration_model_from_configuration('型号：YXQ-50G\n', 'YXQ-50G型')[0] == 'YXQ-50G型'


def test_the_strict_filter_still_drops_digit_heavy_tokens():
    """``strict`` 那条老规则一个字没动：数字比字母多的写法（``B-260型``）在这一格仍不算型号。"""
    assert model_tokens_in_text('型号：B-260型', strict=True) == []
    assert model_tokens_in_text('型号：B-260型') == ['B-260型']


def test_a_different_model_is_still_reported_verbatim():
    """不是同一台机器时一个字都不许换（``bekomat30`` 那条：页面写着 ``BBS-V600``）。"""
    assert configuration_model_from_configuration('型号：BBS-V600\n', 'bekomat30')[0] == 'BBS-V600'


# --------------------------------------------------- 一之五、共前缀缩写行按前缀展开

def test_a_shared_prefix_abbreviation_line_is_expanded():
    """洁定那页：``型号 BW-05H/10H/20H`` 里的 ``BW-10H`` 要能认出来（实测 3 条台账）。"""
    assert model_tokens_in_line('型号 BW-05H/10H/20H') == ['BW-05H', 'BW-10H', 'BW-20H']
    assert configuration_model_from_configuration('型号 BW-05H/10H/20H\n容积 | 10L\n', 'BW-10H') == (
        'BW-10H', '型号 BW-05H/10H/20H')


def test_an_expanded_line_that_names_no_candidate_stays_empty():
    """展开后仍与本候选对不上 → 留空（**不许**取第一个 ``BW-05H``）。"""
    model, reason = configuration_model_from_configuration('型号 BW-05H/10H/20H\n容积 | 10L\n',
                                                           'BW-99H')

    assert model is None
    assert '都不是本候选' in reason
    assert 'BW-05H' in reason


def test_a_full_model_after_a_slash_is_not_given_a_prefix():
    """自带字母头的段（``…/LS-75HV``、``…/B75II``）不补前缀——补了就是猜。"""
    assert model_tokens_in_line('型号 | LS-50HV/LS-75HV/LS-100HV') == [
        'LS-50HV', 'LS-75HV', 'LS-100HV']


def test_the_expansion_is_not_carried_into_the_refetch_gate():
    """回捞门槛仍只看**逐字写着**的那一份（缩写展开只给"配置所属型号"这一格用）。"""
    assert model_tokens_in_text('型号 BW-05H/10H/20H') == ['BW-05H']


# --------------------------------------------------- 二、页面正文里同行／同列的型号

def test_the_model_on_the_same_row_counts():
    page = 'BCV-3S1 洁净工作台 技术参数\n平均风速 | 0.3m/s\n'
    assert configuration_model_from_page(page, '平均风速 | 0.3m/s')[0] == 'BCV-3S1'


def test_the_model_of_the_matching_column_counts():
    """四列参数表：取与这份配置**同一格**的那个型号，不取表头里第一个。

    配置文本的行是从页面原文里抽出来的窗口（真实形状），所以锚点行逐字可定位。
    """
    page = ('型号 | BCV-3S1 | BCV-3S2 | BCV-3S3 | BCV-3S4\n'
            '平均风速 | 0.3 | 0.4 | 0.5 | 0.6\n'
            '洁净等级 | ISO 5 | ISO 5 | ISO 4 | ISO 4\n')
    configuration = ('平均风速 | 0.3 | 0.4 | 0.5 | 0.6\n'
                     '洁净等级 | ISO 5 | ISO 5 | ISO 4 | ISO 4\n')

    # 锚点行是配置的第一行，值落在第 5 格（0.6），表头行第 5 格是 BCV-3S4。
    assert configuration_model_from_page(page, configuration)[0] == 'BCV-3S4'


#: 鑫贝西那张四列参数表：**四列的技术参数值一模一样**（实测台账原件）。
BKQ_TABLE = ('型号 | BKQ-B50II | BKQ-B75II | BKQ-B100II | BKQ-B120II\n'
             '设计温度 | 150℃ | 150℃ | 150℃ | 150℃\n'
             '容积 | 50L | 75L | 100L | 120L\n')


def test_a_multi_model_row_takes_the_one_that_is_the_candidate():
    """四列的值都一样时"值落在第几格"定不了列：取与本候选同一个的那个型号。

    旧代码取第一格（``BKQ-B50II``），于是候选 ``BKQ-B75II`` 的配置被说成属于 B50II。
    """
    model, excerpt = configuration_model_from_page(
        BKQ_TABLE, '设计温度 | 150℃\n', 'BKQ-B75II')

    assert model == 'BKQ-B75II'
    assert excerpt == '型号 | BKQ-B50II | BKQ-B75II | BKQ-B100II | BKQ-B120II'


def test_a_multi_model_row_without_the_candidate_stays_empty():
    """列定不下来、表头里又没有本候选 → 留空（**不取第一列**，也不拿候选顶上）。"""
    model, reason = configuration_model_from_page(
        BKQ_TABLE, '设计温度 | 150℃\n', 'BKQ-B999II')

    assert model is None
    assert '都不是本候选' in reason
    assert 'BKQ-B50II' in reason


def test_the_row_that_names_the_candidate_itself_is_used_when_it_is_ambiguous():
    """同理：配置那一行自己写着多个型号时，只看与本候选同一个的那一个。"""
    page = '上海申安立式灭菌器(LDZH-100KBS,LDZH-150KBS,LDZH-200KBS)\n容积 | 100L\n'
    model, excerpt = configuration_model_from_page(page, '容积 | 100L\n', 'LDZH-150KBS')

    assert model == 'LDZH-150KBS'
    assert excerpt == '上海申安立式灭菌器(LDZH-100KBS,LDZH-150KBS,LDZH-200KBS)'


def test_a_header_with_a_different_cell_count_is_not_guessed():
    """表头行与配置那一行的格数对不上时不敢对齐，留空。"""
    page = ('型号 | BCV-3S1 | BCV-3S2\n'
            '洁净等级 | ISO 5 | ISO 4 | 备注 | 附注\n')
    model, reason = configuration_model_from_page(page, '洁净等级 | ISO 5 | ISO 4 | 备注 | 附注\n')

    assert model is None
    assert '格数对不上' in reason


def test_a_page_without_any_model_row_is_not_guessed():
    page = '洁净等级 | ISO 5\n平均风速 | 0.3\n'
    model, reason = configuration_model_from_page(page, '平均风速 | 0.3\n')

    assert model is None
    assert '没有写着型号' in reason


# --------------------------------------------------- 三、记到候选上（只加字段）

def test_the_candidate_keeps_its_own_model_and_gains_the_configuration_model():
    candidate = {'model': 'bekomat30', 'brand': '济南欧莱博科学仪器有限公司',
                 'excerpt': 'bekomat30 [http://www.sh-gjdq.com]',
                 'configuration': BBS_CONFIGURATION, 'configuration_url': OLAIBO_630,
                 'candidate_id': 'a89c6c393d61c6cc'}
    sources = [_page(OLAIBO_630, BBS_CONFIGURATION)]

    assert record_configuration_model(candidate, sources) is True

    assert candidate['configuration_model'] == 'BBS-V600'
    # 候选自己的型号一个字不改：它是"查这个的价格"的键，id 也不重算。
    assert candidate['model'] == 'bekomat30'
    assert candidate['candidate_id'] == 'a89c6c393d61c6cc'
    basis = candidate['configuration_model_basis']
    assert basis['url'] == OLAIBO_630
    assert basis['excerpt'] == '型号 技术参数 | BBS-V600'


def test_recording_the_configuration_model_is_idempotent():
    candidate = {'model': 'bekomat30', 'configuration': BBS_CONFIGURATION,
                 'configuration_url': OLAIBO_630}
    sources = [_page(OLAIBO_630, BBS_CONFIGURATION)]
    record_configuration_model(candidate, sources)
    before = dict(candidate)

    assert record_configuration_model(candidate, sources) is True
    assert candidate == before


def test_a_candidate_without_a_configuration_gets_no_such_field():
    candidate = {'model': 'BCV-3S1', 'configuration': ''}
    assert record_configuration_model(candidate, []) is False
    assert 'configuration_model' not in candidate


def test_an_unjudged_configuration_model_stays_empty_with_a_reason():
    """判不出就留空，并把原因写在依据里——不拿候选自己的型号顶上冒充。"""
    candidate = {'model': 'BCV-3S1', 'configuration': '技术参数：\n电源：AC220V/50Hz\n',
                 'configuration_url': 'https://shop.test/x'}
    assert record_configuration_model(candidate, [_page('https://shop.test/x', '技术参数：\n电源：AC220V/50Hz\n')]) is False

    assert candidate['configuration_model'] == ''
    assert candidate['configuration_model_basis']['reason']


# --------------------------------------------------- 四、撤下前把已有来源试完

def test_the_refetch_reads_a_page_that_was_already_captured():
    page = _page(BCV_URL, 'BCV-3S1 洁净工作台\n' + BCV_CONFIGURATION)
    spec, attempted = refetch_configuration_from_sources({'sources': [page]},
                                                         {'model': 'BCV-3S1', 'brand': '贝茵'})

    assert spec and spec['url'] == BCV_URL
    assert attempted == [BCV_URL]


def test_a_page_that_never_names_the_candidate_is_not_tried():
    page = _page('https://shop.test/other', '别的型号 XX-9 技术参数\n容积：5L\n')
    spec, attempted = refetch_configuration_from_sources({'sources': [page]},
                                                         {'model': 'BCV-3S1'})

    assert spec is None and attempted == []


def test_the_page_already_used_as_its_configuration_source_is_not_retried():
    page = _page(BCV_URL, 'BCV-3S1 洁净工作台\n' + BCV_CONFIGURATION)
    candidate = {'model': 'BCV-3S1', 'configuration_url': BCV_URL}

    spec, attempted = refetch_configuration_from_sources({'sources': [page]}, candidate)

    assert spec is None and attempted == []


def test_a_grade_word_mistaken_for_a_model_is_not_refetched():
    """真跑里有一条候选叫 ``ISO 5``——那是**洁净等级**被当成型号。

    拿它去回捞，页面上处处都是「洁净等级：ISO 5 级」，会把别人的参数段挂到一个不存在
    的型号头上。这一条比"正文含该候选型号"更窄：型号写法本身得认得出型号。
    """
    page = _page(BCV_URL, 'BCV-3S1 洁净工作台\n' + BCV_CONFIGURATION)
    spec, attempted = refetch_configuration_from_sources({'sources': [page]}, {'model': 'ISO 5'})

    assert spec is None and attempted == []


def test_the_refetch_stops_at_the_page_limit():
    """上限：只试指定页数，不许一条候选把整轮拖住。"""
    pages = [_page('https://shop.test/p%d' % index, 'BCV-3S1 洁净工作台 产品介绍')
             for index in range(4)]

    spec, attempted = refetch_configuration_from_sources({'sources': pages},
                                                         {'model': 'BCV-3S1'}, page_limit=2)

    assert spec is None
    assert attempted == ['https://shop.test/p0', 'https://shop.test/p1']


# --------------------------------------------------- 五、撤除这一步的落点

def test_a_candidate_saved_by_the_refetch_is_not_withdrawn():
    """洁净台那条：``BCV-3S1`` 补到 219 字配置，不再被撤。"""
    page = _page(BCV_URL, 'BCV-3S1 洁净工作台\n' + BCV_CONFIGURATION)
    candidate = {'model': 'BCV-3S1', 'brand': '贝茵',
                 'url': 'https://www.jiancaizhaobiao.cn/news-07fb777eae12ad8cb51d4d88fe73e7e0',
                 'detail_page': {'url': 'https://www.jiancaizhaobiao.cn/news-07fb777eae12ad8cb51d4d88fe73e7e0',
                                 'status': 'fetched', 'coverage': 'model_only'}}
    run = {'sources': [page], 'alternatives': [candidate], 'reference_candidates': [],
           'discovered_candidates': [candidate], 'gaps': []}

    assert withdraw_unconfigured_candidates(run) == []
    assert run['alternatives'] == [candidate]
    assert candidate['configuration_url'] == BCV_URL
    assert candidate['configuration_source'] == 'manufacturer_spec'
    assert candidate['configuration_model'] == 'BCV-3S1'
    assert candidate['configuration_refetch']['attempted'] == [BCV_URL]
    assert any('补到了配置' in gap for gap in run['gaps'])


def test_a_candidate_is_still_withdrawn_when_the_tried_pages_yield_nothing():
    page = _page('https://shop.test/empty', 'BCV-3S1 洁净工作台 产品介绍')
    candidate = {'model': 'BCV-3S1', 'brand': '贝茵'}
    run = {'sources': [page], 'alternatives': [candidate], 'reference_candidates': [],
           'discovered_candidates': [candidate], 'gaps': []}

    withdrawn = withdraw_unconfigured_candidates(run)

    assert withdrawn == [candidate]
    assert run['alternatives'] == []
    # 撤掉的是卡片，不是发现阶段的事实。
    assert run['discovered_candidates'] == [candidate]
    note = [gap for gap in run['gaps'] if '撤下了' in gap][0]
    assert '试过 1 页' in note and 'shop.test' in note
    assert candidate['configuration_refetch']['attempted'] == [page['url']]


def test_both_rosters_are_handled_and_the_sentence_is_written_once():
    empty = {'model': 'X-1', 'brand': ''}
    kept = {'model': 'X-2', 'configuration': '型号 X-2 | 容积 5L'}
    run = {'sources': [], 'alternatives': [empty], 'reference_candidates': [empty, kept],
           'discovered_candidates': [empty], 'gaps': []}

    withdrawn = withdraw_unconfigured_candidates(run)
    withdraw_unconfigured_candidates(run)

    assert withdrawn == [empty, empty]
    assert run['alternatives'] == []
    assert run['reference_candidates'] == [kept]
    assert len([gap for gap in run['gaps'] if '找不到配置就不展示' in gap]) == 1


def test_the_withdrawal_judge_itself_is_unchanged():
    """回捞不是放宽判据：空配置仍然等于没配置，有配置仍然算有配置。"""
    assert candidate_has_configuration({'model': 'X-1', 'configuration': ''}) is False
    assert candidate_has_configuration({'model': 'X-1', 'configuration': '   '}) is False
    assert candidate_has_configuration(
        {'model': 'X-1', 'configuration': '型号 X-1 | 容积 5L'}) is True
