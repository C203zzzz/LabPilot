from backend.services.research_planning import diverse_candidates, discovery_queries, procurement_queries
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
