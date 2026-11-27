"""直读候选带品牌：原文行里写着品牌就带出来，没写就留空。

真跑实测（``sample-d``，111 页文字层资料那一轮）：10 条候选**全部**由正文直读产生
（``proposed_by='text_scan'``），品牌一律留空，于是整批被"品牌未识别"挡在正式候选之外，
正式候选 0 个。而其中一条的原文行是::

    台式常温高速离心机 | 湘智 | G21000 | 2台 | 12,400.00元

——品牌就写在这一行里（同一张表的列序是 ``名称 品牌 规格型号 数量 单价``）。
对照组：同一台机器在模型提出候选的 ``sample-a-rerun2`` 那一轮写的正是 ``brand='湘智'``，
顺利进了正式候选。

这一组用例守两条线：

* 行里写着品牌 → 带出来（三条**只读原文**的路径：同行的表头列名 / 就地的标签 /
  "把型号格、名称格、数量格、单价格都认领之后剩下的那一格"）；
* 行里没有品牌、或那一格不是品牌 → 仍然留空；空的候选照旧进不了正式候选，
  "品牌一致"这条判据一个字没放宽。
"""
from backend.services.procurement_rules import (brand_from_source_line, brand_identity,
                                                brand_name_matches, row_cells)
from backend.services.research_planning import (attach_candidate_suppliers,
                                                diverse_candidates,
                                                extract_supplier_from_text,
                                                text_scan_candidates)

#: ``sample-d`` 那一轮真实的原文行（``sample-a-rerun2`` 的对照轮里它是模型提出的候选，
#: ``brand='湘智'``）。
LINE = '台式常温高速离心机 | 湘智 | G21000 | 2台 | 12,400.00元'
#: 反例素材：同样的表格行，但表里没有品牌这一列。
NO_BRAND_LINE = '台式常温高速离心机 | G21000 | 2台 | 12,400.00元'
BASE = {'brand': '上海天美科学仪器有限公司', 'model': 'UV1100', 'category': '离心机',
        'scenario': '采购一台台式高速离心机'}
PAGE = 'https://www.yiliaozhaobiao.com/news-38fc41b1dcc44392891a1324a0acdbb7'


def _source(text):
    return {'url': PAGE, 'body_status': 'fetched', 'body_text': text}


def test_the_brand_written_in_the_line_is_read_out():
    assert row_cells(LINE) == ['台式常温高速离心机', '湘智', 'G21000', '2台', '12,400.00元']
    assert brand_from_source_line(LINE, model='G21000', category='离心机') == '湘智'


def test_a_line_without_a_brand_cell_stays_empty():
    # 同一个形状、同一批其它格，只是没有品牌那一格：认领完就没有剩余格，读不出品牌。
    assert brand_from_source_line(NO_BRAND_LINE, model='G21000', category='离心机') == ''


def test_the_header_written_in_the_same_line_gives_the_brand_column():
    # ``sample-d`` 那篇公告的搜索摘要：正文整形把 ``<thead>`` 丢了，表头只剩下摘要里这一段。
    line = ('四、主要标的信息 包号：1 名称 品牌 规格型号 数量 单价 '
            '| 小型台式离心机3 | 湘智 | TGL-20MC | 19台 | 14,500.00元')
    assert brand_from_source_line(line, model='TGL-20MC', category='离心机') == '湘智'


def test_a_model_token_that_stops_short_of_the_cell_still_locates_the_row():
    # ``XZ21K-T`` 那一行直读出来的 token 是 ``XZ21K``（``contains_model`` 刻意把 ``-T``
    # 挡在外面），整格对位对不上；此时退到"恰好一格是字母型号形状"。
    line = '台式冷冻高速离心机1 | 湘智 | XZ21K-T | 28台 | 23,000.00元'
    assert brand_from_source_line(line, model='XZ21K', category='离心机') == '湘智'


def test_an_inline_label_is_read_but_a_placeholder_is_not():
    labelled = '台式高速离心机 | 品牌：飞鸽 | H1650 | 2台 | 8,000元'
    assert brand_from_source_line(labelled, model='H1650', category='离心机') == '飞鸽'
    placeholder = '台式高速离心机 | 见附件 | H1650 | 2台 | 8,000元'
    assert brand_from_source_line(placeholder, model='H1650', category='离心机') == ''


def test_navigation_words_are_not_a_brand():
    line = '台式高速离心机 | 品牌库 | 推荐品牌 | H1650 | 2台 | 8,000元'
    assert brand_from_source_line(line, model='H1650', category='离心机') == ''


def test_a_would_be_brand_with_digits_is_refused():
    # 宁漏不错：``3M`` 这类带数字的字号读不出来（数字是型号的形状），
    # 漏一条只少一个候选，收错了就是别家的机器。
    line = '台式高速离心机 | 3M | H1650 | 2台 | 8,000元'
    assert brand_from_source_line(line, model='H1650', category='离心机') == ''


def test_a_line_that_is_not_a_table_row_is_left_alone():
    # 没有分格符的行读不出"哪一列是品牌"——这条路不碰它（既有直读行为不变）。
    assert brand_from_source_line('高速离心机-湘仪H1650台式高速离心机',
                                  model='H1650', category='离心机') == ''


def test_the_alias_table_is_still_the_only_normalisation():
    line = '台式高速离心机 | 湖南湘仪 | H1650 | 2台 | 8,000.00元'
    value = brand_from_source_line(line, model='H1650', category='离心机')
    assert value == '湖南湘仪'                 # 读到的是原文里的写法，这一层不改写
    assert brand_identity(value) == '湘仪'      # 归一是既有别名表的事，没有第二张表


def test_text_scan_candidates_carry_the_brand_and_say_where_it_came_from():
    found = text_scan_candidates([_source(LINE)], BASE)
    assert [(item['model'], item['brand']) for item in found] == [('G21000', '湘智')]
    assert '品牌' in found[0]['note'] and '未经逐项核验' in found[0]['note']


def test_text_scan_candidates_still_leave_the_brand_empty_when_the_line_has_none():
    found = text_scan_candidates([_source(NO_BRAND_LINE)], BASE)
    assert [(item['model'], item['brand']) for item in found] == [('G21000', '')]
    assert '留空而不猜' in found[0]['note']


def test_a_direct_read_candidate_with_a_brand_reaches_the_formal_list():
    # 品牌判据没动：``diverse_candidates`` 仍然只看品牌与型号，带品牌才可能拿到名额。
    found = text_scan_candidates([_source(LINE)], BASE)
    selected, references = diverse_candidates(found, BASE['brand'])
    assert [(item['model'], item['brand']) for item in selected] == [('G21000', '湘智')]
    assert references == []


def test_a_direct_read_candidate_without_a_brand_stays_a_reference():
    found = text_scan_candidates([_source(NO_BRAND_LINE)], BASE)
    selected, references = diverse_candidates(found, BASE['brand'])
    assert selected == []
    assert [item['reference_reason'] for item in references] == ['brand_unresolved']


def test_brand_agreement_is_not_widened():
    # 「品牌一致」这条判据原样不动：``湘智`` 不等于原品牌的全名，空品牌也不等于任何品牌。
    assert brand_name_matches('湘智', '上海天美科学仪器有限公司') is False
    assert brand_name_matches('', '湘智') is False
    assert brand_name_matches('湘智', '湘智') is True


def test_real_sc_page_header_brand_reaches_models_in_a_later_spec_table():
    body = '\n'.join([
        'ZTNEICS', '恒温水浴类仪器', '恒温水浴类仪器', 'THERMOSTATIC BATH',
        '高效率· 低噪音 · 精准', 'High-efficiency·Low noise·Precision',
        '· SC系列 数控超级恒温槽（加热）', '· DC系列 低温恒温槽（加热、制冷）',
        '· GH系列 高精度恒温槽（加热）', '· GDH系列 高精度低温恒温槽（加热、制冷）',
        '· DLK系列 快速低温冷却循环泵（外循环低温冷却槽）', '· SLC恒温冷却循环器',
        '· CK系列 智能型快速高低温程序控制恒温槽', '股票代码：430685',
        '服务热线：4008-122-088', '始于1989',
        '宁波新芝生物科技股份有限公司 国家高新技术企业', '创新服务科学',
        'NINGBO SCIENTZ BIOTECHNOLOGY CO., LTD', '恒温水浴类仪器 恒温水浴类仪器',
        '产品说明', '恒温槽是新芝生物常规产品线之一，也是实验室常规设备。',
        '型 号 | 温度范围 | 工作槽尺寸',
        'SC-5A | 室温+8~95 | 260*140*140',
        'SC-15 | 室温+8~100 | 300*240*200',
        '品牌乙 | 型号 | OTHER-1', 'OTHER-1 | 规格另列',
    ])
    source = {'url': '#', 'original_url': 'https://www.scientz.com/uploads/spec.pdf',
              'body_status': 'fetched', 'body_text': body}
    found = text_scan_candidates([source], {
        'brand': '上海一恒', 'model': 'DK-8D', 'category': '恒温水浴',
        'scenario': '采购恒温水浴锅',
    }, limit=20)

    by_model = {item['model']: item for item in found}
    assert by_model['SC-5A']['brand'] == '宁波新芝生物科技股份有限公司'
    assert by_model['SC-15']['brand'] == '宁波新芝生物科技股份有限公司'
    assert '宁波新芝生物科技股份有限公司' in by_model['SC-15']['brand_evidence']['excerpt']
    assert 'SC-15 | 室温+8~100 | 300*240*200' in by_model['SC-15']['brand_evidence']['excerpt']
    assert by_model['SC-15']['brand_evidence']['original_url'] == source['original_url']
    assert by_model['OTHER-1']['brand'] == ''


def test_syg_models_without_an_explicit_brand_remain_brand_unresolved():
    body = ('恒温水浴锅和恒温水浴广泛应用。 参数 产品型号 | SYG-1210 | SYG-1220 | SYG-1230\n'
            '产品名称 | 6L水浴锅 | 10L水浴锅 | 20L水浴锅')
    base = {'brand': '上海一恒', 'model': 'DK-8D', 'category': '恒温水浴锅',
            'scenario': '采购恒温水浴锅'}
    found = text_scan_candidates([_source(body)], base, limit=20)
    assert {item['model'] for item in found} == {'SYG-1210', 'SYG-1220', 'SYG-1230'}
    assert {item['brand'] for item in found} == {''}
    selected, references = diverse_candidates(found, base['brand'])
    assert selected == []
    assert {item['reference_reason'] for item in references} == {'brand_unresolved'}


def test_supplier_is_read_without_being_written_into_brand():
    body = ('恒温水浴锅 产品型号 | SYG-1210\n'
            '苏州捷美电子有限公司 地址: 苏州市工业园区\n')
    found = text_scan_candidates([_source(body)], {
        'brand': '上海一恒', 'model': 'DK-8D', 'category': '恒温水浴锅',
        'scenario': '采购恒温水浴锅'})
    assert [(item['model'], item['brand'], item['supplier']) for item in found] == [
        ('SYG-1210', '', '苏州捷美电子有限公司')]
    assert found[0]['supplier_evidence']['excerpt'].startswith('苏州捷美电子有限公司')


def test_supplier_extractor_accepts_a_label_without_a_legal_company_suffix():
    assert extract_supplier_from_text('页面关联企业：美国精骐\n') == '美国精骐'


def test_supplier_candidate_can_enter_one_slot_but_unknown_still_stays_reference():
    candidates = [
        {'brand': '', 'supplier': '苏州捷美电子有限公司', 'model': 'SYG-1210'},
        {'brand': '', 'model': 'UNKNOWN-1'},
    ]
    selected, references = diverse_candidates(candidates, '上海一恒', limit=3)
    assert [item['model'] for item in selected] == ['SYG-1210']
    assert [item['reference_reason'] for item in references] == ['brand_unresolved']


def test_existing_brand_wins_and_supplier_is_not_used_as_brand():
    candidate = {'brand': '新芝', 'supplier': '苏州捷美电子有限公司', 'model': 'SC-15'}
    attach_candidate_suppliers([candidate], [])
    assert candidate['brand'] == '新芝'
    assert candidate['supplier'] == '苏州捷美电子有限公司'


def test_a_page_header_brand_can_join_a_model_on_another_line():
    body = '品牌：赛默飞\n恒温水浴锅产品页\n规格型号\nB10 恒温水浴锅\n'
    found = text_scan_candidates([_source(body)],
                                 {'brand': '上海一恒', 'model': 'DK-8D',
                                  'category': '恒温水浴锅', 'scenario': '采购恒温水浴锅'})
    assert [(item['model'], item['brand']) for item in found] == [('B10', '赛默飞')]
    assert '赛默飞' in found[0]['brand_evidence']['excerpt']
    assert 'B10 恒温水浴锅' in found[0]['brand_evidence']['excerpt']


def test_two_header_brands_do_not_cross_apply_to_a_model_row():
    body = ('品牌：赛默飞\n恒温水浴锅产品页\n'
            '另一品牌：某厂\n规格型号\nB10 恒温水浴锅\n')
    found = text_scan_candidates([_source(body)],
                                 {'brand': '上海一恒', 'model': 'DK-8D',
                                  'category': '恒温水浴锅', 'scenario': '采购恒温水浴锅'})
    assert [(item['model'], item['brand']) for item in found] == [('B10', '')]
