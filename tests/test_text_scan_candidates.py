"""正文直读兜底：模型一条候选都没提出来时，候选也不能是 0。

真跑实测（2026-09-27）：窗口正文里明明有 13 行「高速离心机-湘仪H1650台式高速离心机」
这类原文，发现调用返回的却是空数组。这一组用例守住兜底的门槛：只收原文行里读得出来
的型号、不猜品牌、不拿通用设备词表把别的品类（超声波清洗机）当成同类。
"""
from backend.services.research_planning import text_scan_candidates


def _source(url, text, status='fetched'):
    return {'url': url, 'body_status': status, 'body_text': text, 'source_type': 'unknown'}


BASE = {'brand': '湖南凯达', 'model': 'KH20A', 'category': None,
        'scenario': '采购一台 湖南凯达 KH20A 仪器，用于实验室日常检测'}
PAGE = 'https://yiqi.example/detail'
BODY = '\n'.join(['湖南凯达 KH20A台式高速离心机 实拍',
                  '高速离心机-湘仪H1650台式高速离心机',
                  'FC5714高速离心机',
                  'KH22R触屏低温高速冷冻离心机'])


def test_lines_with_another_brand_are_listed_when_the_model_proposed_nothing():
    found = text_scan_candidates([_source(PAGE, BODY)], BASE)
    models = [item['model'] for item in found]
    assert 'H1650' in models and 'FC5714' in models
    hit = next(item for item in found if item['model'] == 'H1650')
    assert hit['excerpt'] == '高速离心机-湘仪H1650台式高速离心机'
    assert hit['url'] == PAGE
    # 它必须自报家门：不是模型提出的、品牌没猜、配置没核。
    assert hit['proposed_by'] == 'text_scan'
    assert hit['brand'] == ''
    assert '未经逐项核验' in hit['note']


def test_the_category_is_read_from_the_line_that_mentions_the_original_model():
    # 场景里只写了"仪器"，没有品类词；但原型号那一行写着"台式高速离心机"。
    assert BASE['category'] is None
    found = text_scan_candidates([_source(PAGE, BODY)], BASE)
    assert found, '品类要能从原型号所在那一行读出来'


def test_the_original_model_and_the_original_brand_are_never_proposed():
    found = text_scan_candidates([_source(PAGE, BODY)], BASE)
    models = [item['model'] for item in found]
    assert 'KH20A' not in models
    assert '湖南凯达' not in models


def test_a_same_family_model_is_kept_but_ranked_last_and_flagged():
    found = text_scan_candidates([_source(PAGE, BODY)], BASE)
    kh22 = next(item for item in found if item['model'] == 'KH22R')
    # 原厂换后缀的型号照列（用户可能想看升级款），但标出来、排在后面。
    assert kh22['same_family_as_original'] is True
    assert found[-1]['same_family_as_original'] is True
    assert found[0]['same_family_as_original'] is False


def test_a_domain_fragment_is_not_mistaken_for_a_model():
    # 实测踩过：导航行里的 ``[https://xinling2021.912688.com]`` 被型号正则抽成"型号"。
    body = '高速离心机 KH20A台式高速离心机 [https://xinling2021.912688.com] 实拍'
    found = text_scan_candidates([_source(PAGE, body)], BASE)
    assert [item['model'] for item in found] == []


def test_another_category_is_not_dragged_in_as_a_substitute():
    # 同一篇正文里成排的超声波清洗机：没有"离心机"的行不能被当成替代品。
    body = '高速离心机 KH20A台式高速离心机\nSG8200T加热脱气超声波清洗机'
    found = text_scan_candidates([_source(PAGE, body)], BASE)
    assert [item['model'] for item in found] == []


def test_without_any_hint_of_the_category_nothing_is_guessed():
    body = '某型号 ABC123 超声波清洗机'
    assert text_scan_candidates([_source(PAGE, body)], dict(BASE, scenario='采购一台仪器')) == []


def test_sources_without_a_body_are_skipped():
    assert text_scan_candidates([_source(PAGE, BODY, status='unavailable')], BASE) == []


def test_a_declared_category_is_used_when_the_scenario_names_one():
    base = dict(BASE, category='离心机')
    found = text_scan_candidates([_source(PAGE, BODY)], base)
    assert {item['model'] for item in found} >= {'H1650', 'FC5714'}
