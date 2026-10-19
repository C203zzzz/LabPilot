# -*- coding: utf-8 -*-
"""读接口把"这个候选项算不算原品牌"算好交给前端。

``ResearchResults.jsx`` 原来自己硬编码了一份只有 ``天津泰斯特``／``上海一恒`` 两条的
别名表（第三份），缺 ``徕卡`` 与两条苏州安泰，界面上把同一家分成两组。
``procurement_research.annotate_candidate_brands`` 把这个判据拿回后端算——别名表的
唯一来源是 ``procurement_rules_improved._BRAND_ALIASES``。这份测试钉它的契约。
"""
import copy

from backend.services.procurement_rules_improved import _BRAND_ALIASES
from backend.services.procurement_research import annotate_candidate_brands, brand_group_key


def test_brand_group_key_uses_the_alias_table():
    assert brand_group_key('天津泰斯特') == '泰斯特'
    assert brand_group_key('徕卡') == 'leica'
    assert brand_group_key('苏州安泰空气技术有限公司') == '苏净安泰'
    assert brand_group_key('') == ''
    assert brand_group_key(None) == ''
    assert brand_group_key(123) == ''


def test_annotates_the_two_fields_the_page_needs():
    run = {'baseline': {'brand': 'Leica'},
           'alternatives': [{'brand': '徕卡', 'model': 'DMi1'},
                            {'brand': 'BINDER', 'model': 'KT 53'},
                            {'brand': '', 'model': 'X-1'}]}
    annotate_candidate_brands(run)
    first, second, third = run['alternatives']
    assert first['brand_key'] == 'leica'
    assert first['same_brand_as_baseline'] is True      # 这是原来界面上被分成两组的那个
    assert second['brand_key'] == 'binder'   # identity 会 casefold，与前端原来的 identity() 一致
    assert second['same_brand_as_baseline'] is False
    assert 'brand_key' not in third and 'same_brand_as_baseline' not in third


def test_the_two_suzhou_spellings_land_in_one_group():
    run = {'baseline': {'brand': '苏净安泰'},
           'alternatives': [{'brand': '苏州安泰'}, {'brand': '苏州安泰空气技术有限公司'}, {'brand': '苏净安泰'}]}
    annotate_candidate_brands(run)
    assert {c['brand_key'] for c in run['alternatives']} == {'苏净安泰'}
    assert all(c['same_brand_as_baseline'] for c in run['alternatives'])


def test_without_a_baseline_brand_everything_is_cross_brand():
    run = {'baseline': {}, 'alternatives': [{'brand': '湘仪'}]}
    annotate_candidate_brands(run)
    assert run['alternatives'][0]['same_brand_as_baseline'] is False


def test_idempotent_and_does_not_touch_anything_else():
    run = {'baseline': {'brand': '泰斯特'}, 'alternatives': [{'brand': '天津泰斯特', 'model': 'SPX-150BIII'}]}
    before = copy.deepcopy(run['alternatives'][0])
    for _ in range(3):
        annotate_candidate_brands(run)
    after = run['alternatives'][0]
    assert after['brand_key'] == '泰斯特'
    assert after['same_brand_as_baseline'] is True
    assert {k: v for k, v in after.items() if k not in {'brand_key', 'same_brand_as_baseline'}} == before


def test_the_empty_alternatives_of_the_old_runs_do_not_break():
    """190 那三次 run 的 ``alternatives`` 就是空列表。"""
    for run in ({'baseline': {'brand': '苏净安泰'}, 'alternatives': []},
                {'baseline': {'brand': '苏净安泰'}},
                {'alternatives': [None, 'x', 3]}):
        annotate_candidate_brands(run)   # 不抛异常即可


def test_it_recomputes_instead_of_keeping_a_stale_value():
    run = {'baseline': {'brand': '泰斯特'}, 'alternatives': [{'brand': '天津泰斯特', 'brand_key': '旧', 'same_brand_as_baseline': '旧'}]}
    annotate_candidate_brands(run)
    assert run['alternatives'][0]['brand_key'] == '泰斯特'
    assert run['alternatives'][0]['same_brand_as_baseline'] is True


def test_the_table_stays_the_only_alias_source():
    """这两个字段的取值只由别名表决定，函数里不许再出现第二条对应关系。"""
    for written, canonical in _BRAND_ALIASES.items():
        assert brand_group_key(written) == canonical
        run = {'baseline': {'brand': canonical}, 'alternatives': [{'brand': written}]}
        annotate_candidate_brands(run)
        assert run['alternatives'][0]['same_brand_as_baseline'] is True
