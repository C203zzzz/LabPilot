from backend.services.research_planning import (configuration_candidate_sort_key,
                                                 diverse_candidates, discovery_queries,
                                                 procurement_queries)
from backend.services.task_title import task_identity


def test_verification_slots_cover_distinct_other_brands():
    rows = [{'brand': b, 'model': m} for b, m in [
        ('Original', 'A10'), ('BINDER', 'B10'), ('binder', 'B20'),
        ('Other', 'C10'), ('Third', 'D10'), ('', 'E10')]]
    selected, references = diverse_candidates(rows, 'original')
    assert [c['model'] for c in selected] == ['B10', 'C10', 'D10']
    assert {c['model']: c['reference_reason'] for c in references} == {
        'A10': 'same_brand', 'B20': 'brand_already_represented', 'E10': 'brand_unresolved'}
    assert diverse_candidates(rows, '')[0] == []


def test_low_parameter_lead_does_not_consume_a_brand_slot():
    selected, references = diverse_candidates([
        {'brand':'Other', 'model':'B10', 'triage_findings':[{'status':'lead_below_requirement'}]},
        {'brand':'Other', 'model':'B20'}], 'Original')
    assert [c['model'] for c in selected] == ['B20']
    assert references[0]['reference_reason'] == 'lead_below_requirement'


def test_single_constraint_search_also_excludes_original_brand():
    baseline = {'brand':'Original', 'category':'恒温水浴', 'model':'B12',
                'parameters':[{'name':'容量', 'value':14, 'unit':'L', 'direction':'min'}]}
    queries = discovery_queries(baseline)
    # Two limiting-constraint queries plus the publication-shaped one: another
    # brand's model number is published in an award notice's detail table, not
    # on the manufacturer pages those two retrieve.
    assert len(queries) == 3
    assert queries[0].startswith('恒温水浴 容量')
    assert any('中标' in q and '报价明细' in q for q in queries)
    assert all('-"Original"' in q for q in queries)
    queries = procurement_queries(baseline)
    assert 'site:edu.cn' in queries[0][0]
    assert all(purpose == 'tender' for _, purpose in queries)


def test_category_metadata_uses_confirmed_baseline_and_never_guesses_from_model():
    assert task_identity({'scenario':'核对 A211'}, baseline={'model':'A211'})['category'] == ''
    inferred = task_identity({'scenario':'仪器核验'}, documents=[
        {'source_name':'manual.pdf', 'extracted_text':'Orion Star A211\nBenchtop pH Meter\nSpecifications'}])
    assert inferred['category'] == 'pH计' and inferred['category_source'] == 'inferred'
    confirmed = task_identity({'scenario':'仪器核验'}, baseline={'category':'pH计', 'brand':'Thermo', 'model':'A211'})
    assert confirmed['title'] == 'pH计选型' and confirmed['category_source'] == 'baseline'
    assert confirmed['model'] == 'A211'


def test_the_reference_reason_separates_our_model_from_the_candidate_line():
    """原型号没品牌 ≠ 候选行没品牌：两种原因分开说，报告才不会把原因说反。

    实测（sample-b-scene2）：5 条候选自己都带品牌，只因原型号 CE-5600A 的品牌没从
    资料里读出来，全被判成 ``brand_unresolved``——报告于是印"原文行里没有可结构化
    提取的品牌"，与事实相反。
    """
    selected, references = diverse_candidates(
        [{'brand': '云奕超声', 'model': 'YF5-1240'}, {'brand': '', 'model': 'AK-009A'}], '')
    assert selected == []
    assert {c['model']: c['reference_reason'] for c in references} == {
        'YF5-1240': 'baseline_brand_unresolved', 'AK-009A': 'brand_unresolved'}


def test_a_reference_item_says_the_new_reason_in_plain_words():
    from backend.services.procurement_research import alternative_report_item

    item = alternative_report_item(
        {'brand': '云奕超声', 'model': 'YF5-1240',
         'reference_reason': 'baseline_brand_unresolved'}, verified=False)

    assert '原型号的厂名没从原文里读到' in item['text']
    assert 'baseline_brand_unresolved' not in item['text']


def test_supplier_only_reference_uses_supplier_in_report_label():
    from backend.services.procurement_research import alternative_report_item

    item = alternative_report_item(
        {'brand': '', 'supplier': '苏州捷美电子有限公司', 'model': 'SYG-1220',
         'reference_reason': 'supplier_already_represented',
         'url': '#', 'original_url': 'https://example.test/syg'}, verified=False)

    assert item['label'] == '供应商：苏州捷美电子有限公司 SYG-1220'
    assert '页面关联供应商已经有别的候选占了名额' in item['text']
    assert item['evidence'][0]['url'] == 'https://example.test/syg'


def test_a_hard_configuration_failure_is_explained_as_a_reference_reason():
    from backend.services.procurement_research import alternative_report_item

    item = alternative_report_item(
        {'brand': '乙厂', 'model': 'WB-FAIL',
         'reference_reason': 'config_hard_constraint',
         'config_match': {'diff_points': ['容积：正文明确低于要求']}}, verified=False)

    assert '配置正文明确不满足已确认要求' in item['text']
    assert 'config_hard_constraint' not in item['text']


def test_configuration_fit_orders_candidates_without_promoting_unknown_brands():
    baseline_brand = '上海一恒'
    rows = [
        {'brand': '', 'model': 'DK-8D-weak', 'config_match': {
            'hard_failures': [], 'soft_score': 0.55, 'confidence': 0.5}},
        {'brand': '', 'model': 'DK-8D-close', 'config_match': {
            'hard_failures': [], 'soft_score': 0.95, 'confidence': 1.0}},
    ]

    ordered = sorted(rows, key=configuration_candidate_sort_key)
    selected, references = diverse_candidates(ordered, baseline_brand)

    assert [c['model'] for c in ordered] == ['DK-8D-close', 'DK-8D-weak']
    assert selected == []
    assert [c['model'] for c in references] == ['DK-8D-close', 'DK-8D-weak']
    assert all(c['reference_reason'] == 'brand_unresolved' for c in references)


def test_configuration_hard_failure_is_reference_but_missing_config_can_advance():
    rows = [
        {'brand': '乙厂', 'model': 'WB-FAIL', 'config_match': {
            'hard_failures': ['容积'], 'soft_score': None, 'confidence': 0.5}},
        {'brand': '丙厂', 'model': 'WB-UNKNOWN', 'config_match': {
            'hard_failures': [], 'soft_score': None, 'confidence': 0.0,
            'basis': 'no_comparable_configuration'}},
    ]

    selected, references = diverse_candidates(
        sorted(rows, key=configuration_candidate_sort_key), '上海一恒')

    assert [c['model'] for c in selected] == ['WB-UNKNOWN']
    assert [(c['model'], c['reference_reason']) for c in references] == [
        ('WB-FAIL', 'config_hard_constraint')]


def test_waterbath_shape_keeps_seventeen_brand_unresolved_leads_visible():
    baseline = {
        'brand': '上海一恒', 'model': 'DK-8D', 'category': '恒温水浴锅',
        'parameters': [
            {'name': '温度范围', 'value': [5, 99], 'unit': '℃', 'direction': 'covers'},
            {'name': '容积', 'value': 8, 'unit': 'L', 'direction': 'equal'},
        ],
    }
    rows = [{
        'brand': '', 'model': f'WB-{index:02d}',
        'config_match': {
            'hard_failures': [], 'soft_score': 0.5 + index / 100,
            'confidence': index / 17,
        },
    } for index in range(17)]

    selected, references = diverse_candidates(
        sorted(rows, key=configuration_candidate_sort_key), baseline['brand'])

    assert selected == []
    assert len(references) == 17
    assert all(item['reference_reason'] == 'brand_unresolved' for item in references)
    assert all(item['model'].startswith('WB-') for item in references)
