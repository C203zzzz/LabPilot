# -*- coding: utf-8 -*-
"""候选身份整理：商标/系列名不当型号、合册型号串认全。

两条规则都来自批3 实测（2026-10-03）：碧云天那轮 ``series`` 与 ``model`` 都填了
``BeyoBath™``（下游拿它当基准，等于拿商标当型号）；博迅净化工作台的型号串
``BJ-1CD/BJ-2CD/BJ-3CD`` 只认出后两个，样本点名的 ``BJ-1CD`` 只出现在引文里。
"""
from backend.services.deepseek_analysis import (PARAMETERS_STATUS_NOT_LOCATED, _family_key,
                                                _is_series_named, _tidy_candidate_identities)


def _candidate(model, brand=None, series=None, configuration='', excerpt=''):
    return {
        'candidate_id': 'analysis-test',
        'brand': brand,
        'series': series,
        'model': model,
        'configuration': configuration,
        'status': 'pending_review',
        'sources': [{'document_id': 'doc', 'excerpt': excerpt}],
        'fields': [],
        'parameters_status': 'extracted',
        'parameters_origin': None,
    }


def test_a_series_name_in_the_model_slot_is_merged_into_the_real_model():
    result = {
        'candidates': [
            _candidate('BeyoBath™', '碧云天/Beyotime', 'BeyoBath™',
                       '数字式恒温水浴锅(双列四孔，防干烧)，1套；含主机；订货号 E6890'),
            _candidate('E6890', '碧云天 Beyotime', 'BeyoBath™',
                       '数字式恒温水浴锅(双列四孔, 防干烧)'),
        ],
        'gaps': [],
    }
    _tidy_candidate_identities(result)
    assert [c['model'] for c in result['candidates']] == ['E6890']
    assert '订货号 E6890' in result['candidates'][0]['configuration']
    assert any('BeyoBath' in gap for gap in result['gaps'])


def test_a_normal_model_is_left_alone():
    result = {'candidates': [_candidate('E6890', '碧云天 Beyotime', 'BeyoBath™', '双列四孔')],
              'gaps': []}
    _tidy_candidate_identities(result)
    assert [c['model'] for c in result['candidates']] == ['E6890']
    assert result['gaps'] == []


def test_a_series_name_without_a_sibling_is_left_in_place():
    """找不到同系列的型号候选时不动它：资料里出现过的名字不许静默删掉。"""
    result = {'candidates': [_candidate('BeyoBath™', '碧云天', 'BeyoBath™', '双列四孔')],
              'gaps': []}
    _tidy_candidate_identities(result)
    assert [c['model'] for c in result['candidates']] == ['BeyoBath™']


def test_a_family_model_seen_only_in_the_excerpt_becomes_a_candidate():
    result = {
        'candidates': [_candidate('BJ-2CD', '上海博迅医疗生物仪器股份有限公司', None,
                                  '工作区配置紫外线灭菌灯',
                                  excerpt='BJ-1CD/BJ-2CD/BJ-3CD 净 化 工 作 台')],
        'gaps': [],
    }
    _tidy_candidate_identities(result)
    models = [c['model'] for c in result['candidates']]
    assert 'BJ-1CD' in models and 'BJ-3CD' in models
    completed = [c for c in result['candidates'] if c.get('completed_from_family')]
    assert completed
    # 补出来的候选不照抄同族的参数：那些尺寸是另一台机器的。
    assert all(all(field['value'] == 'unknown' for field in candidate['fields'])
               for candidate in completed)
    # 也没为它跑过参数抽取——如实标成"没跑"，判据据此判「不适用」而不是「产品没做到」。
    assert all(candidate['parameters_status'] == PARAMETERS_STATUS_NOT_LOCATED
               for candidate in completed)
    assert any('BJ-1CD' in gap for gap in result['gaps'])


def test_measurements_beside_a_family_model_are_not_models():
    result = {
        'candidates': [_candidate('BJ-2CD', '上海博迅', None, '工作区配置紫外线灭菌灯',
                                  excerpt='4．外形尺寸：BJ-1CD（990×744×1680）、'
                                          'BJ-2CD（1460×744×1680）；')],
        'gaps': [],
    }
    _tidy_candidate_identities(result)
    models = [c['model'] for c in result['candidates']]
    assert models.count('BJ-1CD') == 1
    assert not any('×' in model or model.isdigit() for model in models)


def test_an_unrelated_model_in_the_same_excerpt_is_not_adopted():
    result = {
        'candidates': [_candidate('BJ-2CD', '上海博迅', None, '工作区配置紫外线灭菌灯',
                                  excerpt='BJ-2CD 净化工作台；配套 SW-CJ-1FDG 超净台，'
                                          '另有 ME204E 天平')],
        'gaps': [],
    }
    _tidy_candidate_identities(result)
    assert [c['model'] for c in result['candidates']] == ['BJ-2CD']


def test_family_key_ignores_case_and_separators():
    assert _family_key('BJ-1CD') == _family_key('bj 2cd')
    assert _is_series_named({'model': 'BeyoBath™', 'series': 'BeyoBath™'})
    assert not _is_series_named({'model': 'E6890', 'series': 'BeyoBath™'})
