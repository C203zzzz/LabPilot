from backend.services.procurement_rules import comparison_basis, similarity_assessment


def _row(name, status, evidence_status='confirmed', conflict=False):
    return {
        'required': {'name': name, 'value': 1, 'unit': 'unit'},
        'status': status,
        'evidence_status': evidence_status,
        'conflict': conflict,
    }


def test_seventy_percent_threshold_allows_comparison_with_explicit_difference():
    result = similarity_assessment([
        _row('capacity', 'satisfied'),
        _row('temperature', 'satisfied'),
        _row('recording', 'satisfied'),
        _row('power', 'violated'),
    ])

    assert result['similarity_score'] == 0.75
    assert result['comparison_disposition'] == 'comparable_candidate'
    assert [item['name'] for item in result['satisfied_parameters']] == ['capacity', 'temperature', 'recording']
    assert [item['name'] for item in result['difference_parameters']] == ['power']
    assert result['missing_parameters'] == []


def test_unknown_is_not_a_match_and_remains_a_gap():
    result = similarity_assessment([
        _row('capacity', 'satisfied'),
        _row('temperature', 'unknown'),
    ])

    assert result['similarity_score'] == 1.0
    assert result['similarity_counts'] == {
        'satisfied': 1, 'different': 0, 'missing': 1, 'comparable': 1, 'total': 2,
    }
    assert result['comparison_disposition'] == 'comparable_candidate'
    assert [item['name'] for item in result['missing_parameters']] == ['temperature']


def test_provisional_match_does_not_count_as_verified_similarity():
    result = similarity_assessment([
        _row('capacity', 'satisfied', evidence_status='provisional'),
        _row('temperature', 'satisfied'),
        _row('recording', 'violated'),
    ])

    assert result['similarity_score'] == 0.5
    assert result['comparison_disposition'] == 'needs_review'
    assert [item['name'] for item in result['missing_parameters']] == ['capacity']


def test_fully_verified_match_has_recommendation_comparison_disposition():
    result = similarity_assessment([_row('capacity', 'satisfied'), _row('temperature', 'satisfied')])

    assert result['similarity_score'] == 1.0
    assert result['comparison_disposition'] == 'recommended_comparison'


def test_no_confirmed_parameter_is_not_a_full_score():
    """用户没有必须满足的实验要求时，0/0 不能被算成 100% 满足。"""
    result = similarity_assessment([], confirmed_parameter_count=0)

    assert result['comparison_basis'] == 'no_confirmed_parameter'
    assert result['confirmed_parameter_count'] == 0
    assert result['parameter_comparison_performed'] is False
    assert result['similarity_score'] is None
    assert result['comparison_disposition'] == 'needs_review'
    assert result['similarity_counts'] == {'satisfied': 0, 'different': 0, 'missing': 0, 'comparable': 0, 'total': 0}
    assert '没有确认必须满足的实验要求' in result['difference_summary']


def test_parameter_row_without_a_matrix_is_still_a_review_case():
    """矩阵里全是 unknown 时（有指标但查不到），结论也不是"没有指标"。"""
    result = similarity_assessment([_row('capacity', 'unknown')], confirmed_parameter_count=1)

    assert result['comparison_basis'] == 'confirmed_parameters'
    assert result['parameter_comparison_performed'] is True
    assert result['comparison_disposition'] == 'needs_review'
    assert '没有确认必须满足的实验要求' not in result['difference_summary']


def test_comparison_basis_counts_only_named_parameters():
    assert comparison_basis([]) == {'comparison_basis': 'no_confirmed_parameter', 'confirmed_parameter_count': 0}
    assert comparison_basis(None) == {'comparison_basis': 'no_confirmed_parameter', 'confirmed_parameter_count': 0}
    assert comparison_basis([{'value': 1}]) == {'comparison_basis': 'no_confirmed_parameter', 'confirmed_parameter_count': 0}
    assert comparison_basis([{'name': '温度', 'value': 1}]) == {'comparison_basis': 'confirmed_parameters', 'confirmed_parameter_count': 1}


def test_alternative_conclusion_never_claims_compliance_without_parameters():
    from backend.services.procurement_rules import alternative_conclusion

    candidate = {'status': 'no_comparison_basis', 'comparison_disposition': 'needs_review',
                 'comparison_basis': 'no_confirmed_parameter', 'matrix': []}
    conclusion = alternative_conclusion({}, [], candidate)

    # 结论只能说"没比过"，不能出现任何"满足"式的判定。
    assert conclusion == ('本轮没有确认必须满足的实验要求，未做逐项参数比对；'
                          '只能按价格与来源比较，不能判定关键参数满足或不满足')
    for claim in ('全部已列关键指标均有可核验证据', '推荐比较', '可比较候选', '关键参数满足。'):
        assert claim not in conclusion
