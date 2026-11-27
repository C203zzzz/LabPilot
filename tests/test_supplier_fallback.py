# -*- coding: utf-8 -*-
"""品牌缺失时的候选身份回退：供应商可展示，但不能冒充品牌。"""

from backend.services.research_planning import (
    attach_candidate_suppliers,
    diverse_candidates,
    extract_supplier_from_text,
    supplier_from_candidate_sources,
)


def test_extracts_supplier_from_labelled_or_contact_line():
    assert extract_supplier_from_text('供应商：苏州捷美电子有限公司') == '苏州捷美电子有限公司'
    assert extract_supplier_from_text('苏州捷美电子有限公司 地址:') == '苏州捷美电子有限公司'


def test_does_not_treat_navigation_or_unrelated_text_as_supplier():
    text = '品牌\n相关产品\n相关标准：GB/T 12345\n产品型号 | SYG-1210'
    assert extract_supplier_from_text(text) == ''


def test_does_not_turn_company_description_or_table_header_into_supplier():
    assert extract_supplier_from_text(
        '点成生物是一家专注于生物和医药领域一站式解决方案的公司（前身是虹科生物科技事业部）。'
    ) == ''
    assert extract_supplier_from_text('生产厂家 | 数量 | 单价 | 总价') == ''


def test_does_not_turn_navigation_value_into_supplier_but_keeps_explicit_company_alias():
    assert extract_supplier_from_text('供应商联系：联系') == ''
    assert extract_supplier_from_text('页面关联企业：美国精骐') == '美国精骐'


def test_keeps_a_real_company_name_from_a_page_with_company_description():
    text = (
        '点成生物是一家专注于生物和医药领域一站式解决方案的公司。\n'
        '广州点成生物科技有限公司 地址：广州市黄埔区\n'
    )
    assert extract_supplier_from_text(text) == '广州点成生物科技有限公司'


def test_accepts_a_company_footer_with_copyright_marker():
    assert extract_supplier_from_text('广州点成生物科技有限公司 | 版权所有') == '广州点成生物科技有限公司'


def test_does_not_pick_an_unrelated_company_from_a_long_table_page():
    text = (
        '序号 | 货物名称 | 品牌 | 型号 | 生产厂家 | 数量 | 单价 | 总价\n'
        '7 | 制冰机 | Scotsman | AF 103 | 意大利Scotsman公司 | 1台 | 35000 | 35000\n'
        '中标单位：兰州冉天工贸有限公司\n'
    )
    assert extract_supplier_from_text(text) == ''


def test_does_not_use_procurement_party_as_product_supplier():
    assert extract_supplier_from_text('中标单位：兰州冉天工贸有限公司') == ''
    assert extract_supplier_from_text('六、中标供应商及中标金额：') == ''
    assert extract_supplier_from_text(
        '甘肃西招国际招标有限公司受中国农业科学院兰州兽医研究所的委托，对中国农业科学院兰州兽医研究所 2019 年科研设备采购项目以公开招标方式进行采购，评审委员会确定中标结果。'
    ) == ''


def test_supplier_is_read_only_from_the_candidate_source_and_kept_separate():
    url = 'https://www.cnreagent.com/product/pro_54560.html'
    candidate = {
        'brand': '', 'model': 'SYG-1210', 'url': '#', 'original_url': url,
    }
    sources = [{
        'url': '#', 'original_url': url, 'body_status': 'fetched',
        'body_text': '恒温水浴锅\n产品型号 | SYG-1210\n苏州捷美电子有限公司 地址:',
    }]
    supplier, evidence = supplier_from_candidate_sources(candidate, sources)
    assert supplier == '苏州捷美电子有限公司'
    assert evidence['original_url'] == url
    assert evidence['excerpt'] == '苏州捷美电子有限公司 地址:'

    attach_candidate_suppliers([candidate], sources)
    assert candidate['brand'] == ''
    assert candidate['supplier'] == '苏州捷美电子有限公司'


def test_existing_brand_wins_and_supplier_is_not_added():
    candidate = {'brand': '上海一恒', 'model': 'DK-8D', 'url': 'https://example.test/a'}
    attach_candidate_suppliers([candidate], [{
        'url': candidate['url'], 'body_text': '苏州捷美电子有限公司 地址:',
    }])
    assert candidate == {'brand': '上海一恒', 'model': 'DK-8D', 'url': 'https://example.test/a'}


def test_attach_cleans_stale_supplier_and_reextracts_from_candidate_source():
    url = 'https://example.test/waterbath'
    candidate = {
        'brand': '', 'model': 'Aqua 12', 'url': url,
        'supplier': '数量',
        'supplier_evidence': {'url': url, 'excerpt': '数量'},
    }
    attach_candidate_suppliers([candidate], [{
        'url': url,
        'body_text': '序号 | 品牌 | 型号 | 生产厂家 | 数量\nAqua 12 | 12 L',
    }])
    assert candidate.get('supplier') is None
    assert candidate.get('supplier_evidence') is None


def test_attach_cleans_description_but_keeps_legal_company_from_source():
    url = 'https://example.test/waterbath'
    candidate = {
        'brand': '', 'model': 'Aqua 12', 'url': url,
        'supplier': '点成生物是一家专注于生物和医药领域一站式解决方案的公司',
        'supplier_evidence': {'url': url, 'excerpt': '旧值'},
    }
    attach_candidate_suppliers([candidate], [{
        'url': url,
        'body_text': '点成生物是一家专注于生物和医药领域一站式解决方案的公司\n'
                     '广州点成生物科技有限公司 地址：广州市黄埔区',
    }])
    assert candidate['supplier'] == '广州点成生物科技有限公司'
    assert candidate['supplier_evidence']['excerpt'].startswith('广州点成生物科技有限公司')


def test_attach_preserves_existing_legal_supplier():
    candidate = {
        'brand': '', 'model': 'SYG-1210',
        'supplier': '广州点成生物科技有限公司',
        'supplier_evidence': {'url': 'https://example.test/a'},
    }
    attach_candidate_suppliers([candidate], [])
    assert candidate['supplier'] == '广州点成生物科技有限公司'


def test_supplier_only_candidates_can_use_one_slot_per_supplier_without_brand_grouping():
    rows = [
        {'brand': '', 'supplier': '苏州捷美电子有限公司', 'model': 'SYG-1210'},
        {'brand': '', 'supplier': '苏州捷美电子有限公司', 'model': 'SYG-1220'},
        {'brand': '', 'supplier': '另一家公司', 'model': 'WB-20'},
    ]
    selected, references = diverse_candidates(rows, '上海一恒', limit=3)
    assert [item['model'] for item in selected] == ['SYG-1210', 'WB-20']
    assert [(item['model'], item['reference_reason']) for item in references] == [
        ('SYG-1220', 'supplier_already_represented'),
    ]
    assert all(item.get('brand', '') == '' for item in selected)
