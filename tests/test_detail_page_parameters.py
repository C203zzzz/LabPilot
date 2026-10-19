"""候选详情页参数抽取：从候选原文行里的详情页网址，读出那台仪器的真实规格。

这一组用例守住四件事：
  1. **引用逐字可核对**：抽出来的每个值都带原文行，且那行能在页面正文里原样找到；
  2. **读不到就是未知**：正文里没有这一项时，绝不从同类产品的常识补、也不从别的型号行借；
  3. **别的型号的数字不串台**：同一页上另一台仪器的转速不能算到这个候选头上；
  4. **两种详情页写法都要读得出来**：紧凑规格行（``200–15000rpm，21379xg，Max6×250ml``）
     与整机参数句／转子表（``上限转速可达17500r/min``）。

夹具全部是**真实页面上的原文行**（``yiqi.com`` 详情页），不是手写编出来的。
"""
import pytest

from backend.services.research_planning import (
    configuration_alignment, compact_spec_lines, detail_page_parameters, detail_page_url,
    other_models_in, spec_rotor_rows,
)

LISTING = 'https://www.yiqi.com/chanpin/1.html'
DETAIL = 'https://www.yiqi.com/product/detail_1000001.html'
#: 目录页候选行的原样（实测）。那一行**只有型号和品类，没有任何数值** ——
#: 这正是"配置比配置"读不到东西的原因，也是详情页那条链的起点。
LISTING_LINE = 'FC5816高速离心机 [%s]' % DETAIL
FC_SPEC_LINE = '200–15000rpm，21379xg，Max6×250ml'
H1750_INTRO = ('产品介绍： H1750台式高速离心机体积小巧、高度适中、13款不同容量转子可满足广泛的离心应用，'
               '其中有微量离心管、PCR排管、微孔板，15/50ml尖底离心管和采血管等转子、24x1.5ml'
               '上限转速可达17500r/min，其离心性能不逊色于大型台式离心机。')
#: 同一段正文里，离心力的写法与转速并列（实测那一行两个值都写了）。
H1750_INTRO_WITH_RCF = H1750_INTRO + ' 上限相对离心力可达29302×g。'
ROTOR_ROW = ('50/60Hz 整机功率 700W 外形尺寸（W×D×H) 380×505×360(mm) 包装尺寸（W×D×H) 480×780×460(mm) '
             '重量 32kg 转子 参数 型号 产品名称 上限转速（r/min） 上限相对离心力(×g) 容量 '
             'No.1 角转子 17500 29302 24×1.5ml/2ml No.2 角转子 15000 15840 12×5ml '
             'No.3 角转子 13000 17370 12×10ml')

SPEED = {'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'}
RCF = {'name': '最大相对离心力', 'value': 25000, 'unit': '×g', 'direction': 'min'}
KIND = {'name': '类型', 'value': '高速冷冻离心机', 'direction': 'equal'}


def _page(model, detail_id):
    return '「%s高速离心机」价格、参数、图片 - 仪器网\n仪器网（yiqi.com）欢迎您！\n' % model, \
        'https://www.yiqi.com/product/detail_%s.html' % detail_id


def _detail_body(model, lines):
    head, _ = _page(model, '1')
    return head + '\n'.join(lines)


def _run(model, requirements, detail_lines, listing_line=LISTING_LINE):
    """按产品真实形状跑一次：候选在目录页那一行，规格在详情页。"""
    url = 'https://www.yiqi.com/product/detail_9.html'
    sources = [
        {'url': LISTING, 'body_text': listing_line, 'body_status': 'fetched'},
        {'url': url, 'body_text': _detail_body(model, detail_lines),
         'body_status': 'fetched', 'purpose': 'alternatives_detail'},
    ]
    candidate = {'model': model, 'url': LISTING, 'excerpt': listing_line,
                 'detail_page': {'url': url, 'status': 'fetched'}}
    return configuration_alignment(candidate, sources, requirements)


def _extract(model, requirements, detail_lines):
    url = 'https://www.yiqi.com/product/detail_9.html'
    return detail_page_parameters(requirements, [(url, _detail_body(model, detail_lines), model)])


def test_the_detail_page_url_comes_from_the_candidate_line_itself():
    candidate = {'model': 'FC5816', 'url': LISTING, 'excerpt': LISTING_LINE}
    assert detail_page_url(candidate) == DETAIL
    # 行里没写网址时**不猜**：返回空串，调用方据此记"没有找到详情页"。
    assert detail_page_url({'model': 'FC5816', 'url': LISTING, 'excerpt': 'FC5816高速离心机'}) == ''
    # 别的行上的网址与这个候选无关。
    other = {'model': 'FC5816', 'url': LISTING, 'excerpt': 'H1750高速离心机 [%s]' % DETAIL}
    assert detail_page_url(other) == ''


def test_a_compact_spec_line_yields_the_two_numbers_and_their_exact_line():
    found, notes = _extract('FC5816', [SPEED, RCF], [FC_SPEC_LINE])
    assert notes[0]['compact_spec_lines'] == 1
    assert found['最高转速']['observed']['value'] == 15000.0
    # 单位归一化到 ``procurement_rules.UNITS`` 里的写法：页面印 ``rpm``，系统里是 ``r/min``
    # （两者是同一单位的两种写法；归一化前单位写法不同会被判"未知"，等于读到了也丢掉）。
    assert found['最高转速']['observed']['unit'] == 'r/min'
    assert found['最大相对离心力']['observed']['value'] == 21379.0
    # 引用必须是页面正文里原样的一行（逐字可核对）。
    body = _detail_body('FC5816', [FC_SPEC_LINE])
    assert found['最高转速']['observed']['evidence']['excerpt'] in body
    assert found['最高转速']['observed']['evidence']['excerpt'] == FC_SPEC_LINE


def test_a_range_is_read_as_its_upper_bound_because_the_requirement_is_a_maximum():
    # ``200–15000rpm`` 表达的是"最高 15000"；读成下界 200 会把一台满足要求的机器判成不满足。
    found, _ = _extract('FC5816', [SPEED], [FC_SPEC_LINE])
    assert found['最高转速']['observed']['value'] == 15000.0


def test_a_whole_machine_sentence_is_read_with_its_own_units():
    found, _ = _extract('H1750', [SPEED, RCF], [H1750_INTRO_WITH_RCF])
    assert found['最高转速']['observed']['value'] == 17500.0
    assert found['最高转速']['observed']['unit'] == 'r/min'
    assert found['最大相对离心力']['observed']['value'] == 29302.0
    assert found['最大相对离心力']['observed']['unit'] == '×g'


def test_a_rotor_table_is_read_by_column_order_not_by_guessing():
    rows = spec_rotor_rows([ROTOR_ROW], 'H1750')
    # 三个转子的（转速, 相对离心力）都取到了；容量列的小数绝不能混进来。
    assert [(row[0], row[1]) for row in rows] == [(17500.0, 29302.0), (15000.0, 15840.0), (13000.0, 17370.0)]


def test_another_models_numbers_on_the_same_page_never_count_for_this_candidate():
    # 目录页那一行只有型号，详情页讲的是**另一个型号**：不能借它的数字。
    found, _ = _extract('H1650K', [SPEED], [H1750_INTRO])
    assert found == {}
    assert other_models_in(H1750_INTRO, 'H1650K') == ['H1750']


def test_a_missing_parameter_is_simply_absent_not_invented():
    found, notes = _extract('FC5816', [SPEED, RCF, KIND], [FC_SPEC_LINE])
    assert sorted(found) == ['最大相对离心力', '最高转速']
    assert '类型' not in found, '正文里没读到类型，就不能拿"高速离心机"顶上去'
    assert notes[0]['has_spec_area'] is True


def test_a_page_without_any_spec_area_reports_that_it_has_none():
    # 只有品类、没有任何数值的页面（实测 C10K 那一页的产品介绍就是这样）。
    found, notes = _extract('C10K', [SPEED, RCF], ['产品介绍: C10K-230V系列迷你离心机外观新颖独特，灵巧多用。'])
    assert found == {}
    assert notes[0]['has_spec_area'] is False
    assert notes[0]['body_chars'] > 0


def test_the_listing_line_alone_still_yields_nothing_which_is_why_the_detail_page_is_needed():
    result = _run('FC5816', [SPEED, RCF], [FC_SPEC_LINE], listing_line=LISTING_LINE)
    # 有详情页时读得到；这里改跑"目录页那一行"来证明它自己读不到任何数值。
    listing_only = configuration_alignment(
        {'model': 'FC5816', 'url': LISTING, 'excerpt': LISTING_LINE},
        [{'url': LISTING, 'body_text': LISTING_LINE, 'body_status': 'fetched'}],
        [SPEED, RCF])
    assert [item['status'] for item in listing_only['items']] == ['unknown', 'unknown']
    assert listing_only['soft_score'] is None
    assert result['confidence'] == 1.0


def test_alignment_uses_the_detail_page_and_keeps_the_exact_line_as_evidence():
    result = _run('H1750', [SPEED, RCF], [H1750_INTRO_WITH_RCF])
    assert [item['status'] for item in result['items']] == ['violated', 'satisfied']
    speed = result['items'][0]['observed']
    assert speed['value'] == 17500.0 and speed['unit'] == 'r/min'
    assert speed['evidence']['url'].endswith('detail_9.html')
    assert speed['evidence']['excerpt'] in _detail_body('H1750', [H1750_INTRO_WITH_RCF])
    # 读到且满足的那一项（离心力 29302 ≥ 25000）撑起了软相似度：超出要求越多越不像"同档"，
    # 但满足本身是加分项，所以 (0.29302-0.25)/0.25 → 1/(1+0.172) 这个量级。
    assert result['soft_score'] == pytest.approx(0.85318, abs=1e-4)
    assert result['confidence'] == 1.0


def test_navigation_urls_in_the_header_do_not_disable_the_spec_line():
    """真跑踩过：浏览器渲染后的页头里，导航与面包屑都带方括号网址。

    网址 slug 长得很像型号（``list11``／``zt000``／``sort01``），被当成"别的型号"之后
    整页紧凑规格行被禁用 —— 真跑 10 条候选因此抽到 0 项。判"这一页讲的是不是本品"之前
    必须先把网址剥掉。
    """
    body = _detail_body('FC5816', [
        '网站首页 [https://www.yiqi.com/] -资讯 [https://www.yiqi.com/news/] '
        '-产品 [https://www.yiqi.com/product/]',
        '仪器大全 [https://www.yiqi.com/product/list11.html]',
        '耗材配件 [https://www.yiqi.com/product/list01.html]',
        '仪器网 [https://www.yiqi.com/] > 产品中心 [https://www.yiqi.com/product/] > '
        '某公司 [https://www.yiqi.com/zt000/] > FC5816高速离心机',
        FC_SPEC_LINE,
    ])
    model, requirements = 'FC5816', [SPEED, RCF]
    assert other_models_in('\n'.join(body.splitlines()[:30]), model) == []
    found, _ = detail_page_parameters(requirements, [('u', body, model)])
    assert found['最高转速']['observed']['value'] == 15000.0
    assert found['最大相对离心力']['observed']['value'] == 21379.0


def test_thousand_separators_are_read_as_one_number():
    """真跑踩过：``800-14,500rpm`` 读成 500rpm、``14,000xg`` 读成 0xg。

    产品页就是这么写千分位的；不认逗号会把上限读小一个数量级，直接影响"够不够"的判定。
    """
    line = 'C1450-230V 上限转速： 800-14,500rpm (in1000rpm increments)增量'
    found, _ = _extract('C1450', [SPEED], [line])
    assert found['最高转速']['observed']['value'] == 14500.0

    rcf_line = 'C1450-230V (RCF)上限相对离心力： 14,000xg'
    found, _ = _extract('C1450', [RCF], [rcf_line])
    assert found['最大相对离心力']['observed']['value'] == 14000.0


def test_a_model_suffix_token_is_not_another_model():
    """``FC5513-R01`` 被拆开后的 ``R01`` 不能算"外来型号"（真跑踩过：整页被禁用）。"""
    head = 'FC5513-R01微量高速离心机 型号 FC5513-R01 品牌 R01'
    assert other_models_in(head, 'FC5513') == []


def test_a_rendered_table_row_is_quoted_verbatim():
    """浏览器渲染后的表格会被摊成"表格按列展开"的一行，引用照样逐字可核对。"""
    row = '[表格按列展开 行3列2] H1750 上限相对离心力 29302xg'
    found, _ = _extract('H1750', [RCF], [row])
    assert found['最大相对离心力']['observed']['value'] == 29302.0
    assert found['最大相对离心力']['observed']['evidence']['excerpt'] == row


def test_a_satisfied_requirement_now_produces_a_real_soft_score():
    # 要求 15000 时 FC5816 的 15000 rpm 正好满足：软相似度应当是实数，不再是 None。
    result = _run('FC5816', [{'name': '最高转速', 'value': 15000, 'unit': 'r/min', 'direction': 'min'}],
                  [FC_SPEC_LINE])
    assert result['items'][0]['status'] == 'satisfied'
    assert result['soft_score'] == pytest.approx(1.0)
    assert result['confidence'] == 1.0
