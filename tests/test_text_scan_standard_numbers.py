# -*- coding: utf-8 -*-
"""标准号不是型号：正文兜底不许把 ``T/CIS 17009-2025`` 抠成候选 ``CIS 17009``。

取证：``sample-b-brand1`` 唯一的候选
``CIS 17009`` 来自这一行::

    T/CIS 17009-2025-槽式超声波清洗机通用技术规范-国家标准馆·国家数字标准馆

``_MODEL_TOKEN`` 把 ``T/`` 与 ``-2025`` 留在匹配之外，于是抠出了 ``CIS 17009``——
一条品牌为空、挤进参考候选的误召回。

反过来**必须验证不误伤**：纯字母＋数字的真型号（``CIS 17009`` 本身、``SB-5200DTS``）
照旧要被召回来。排除规则只认"标准机构前缀 + 编号"这个形态，不认"字母＋数字"。
"""
from backend.services.research_planning import text_scan_candidates

BASE = {'brand': '甲厂', 'model': 'Q10', 'category': '超声波清洗机',
        'scenario': '采购一台超声波清洗机'}
PAGE = 'https://ndls.example/standard/detail/6f0fc71b9eaf52e491348efa53140004'


def _source(text):
    return {'url': PAGE, 'body_status': 'fetched', 'body_text': text, 'source_type': 'unknown'}


def _models(*lines):
    found = text_scan_candidates([_source('\n'.join(lines))], BASE)
    return [item['model'] for item in found]


# ---------------------------------------------------------------------------
# 标准号形态：一条候选都不许产出
# ---------------------------------------------------------------------------

def test_the_exact_line_from_the_evidence_yields_no_candidate():
    assert _models('T/CIS 17009-2025-槽式超声波清洗机通用技术规范-国家标准馆·国家数字标准馆') == []


def test_national_and_industry_standards_are_not_models():
    assert _models('GB/T 12345-2020 槽式超声波清洗机技术条件') == []
    assert _models('JB/T 5678 超声波清洗机') == []
    assert _models('ISO 9001 超声波清洗机技术规范') == []
    assert _models('GB 12345 超声波清洗机技术条件') == []


def test_a_standard_number_next_to_a_real_model_leaves_the_model_alone():
    models = _models('槽式超声波清洗机 依据 T/CIS 17009-2025 选用 CIS 17009 技术参数')
    assert models == ['CIS 17009'], models


# ---------------------------------------------------------------------------
# 误伤验证：形似的真型号照旧召回来
# ---------------------------------------------------------------------------

def test_a_plain_letter_number_model_is_still_found():
    assert _models('槽式超声波清洗机 CIS 17009 技术参数 容量 100 L') == ['CIS 17009']


def test_hyphenated_and_suffixed_models_are_still_found():
    models = _models('槽式超声波清洗机 乙厂 SB-5200DTS 技术参数',
                     '槽式超声波清洗机 丙厂 KH20A 技术参数')
    assert set(models) == {'SB-5200DTS', 'KH20A'}, models


def test_a_model_that_merely_contains_a_standard_like_number_is_kept():
    """``17009`` 只是型号里的数字，没有标准机构前缀——不许被当成标准号。"""
    assert _models('槽式超声波清洗机 CIS17009 技术参数') == ['CIS17009']
