"""候选池非空、却没有可比较候选时，``gaps`` 那句话要按真实原因说。

``设计规范`` §2：``b281-hve50``（2026-10-08）与 ``b279-reg-me204e``（2026-10-08）两轮都
打出了"本轮没有达到70%已核验相似度的跨品牌候选。"，而这两轮**没有任何候选算过那个
分数**（10 条/13 条候选的 ``comparison_disposition`` 全是 ``None``）。真正的拦路者是
准入：``b281-hve50`` 是 3 条模型提案在有效容积上确实不符（配置原文 85/110/75 L，要求
50 L）+ 8 条线索品牌读不到；``b279-reg-me204e`` 是 3 条跨品牌提案读不到配置被撤下。
把"身份/证据缺失"和"配置明确不符"统一说成"相似度没达到"，读者会以为"再像一点就能进
比较"——对外的粒度粗于内部事实（``设计规范`` §3.4/§5）。
"""
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FLAT = '本轮没有达到70%已核验相似度的跨品牌候选。'


def _finished_run(service, run, references, alternatives=()):
    """把这一轮摆成"两条车道都跑完、替代车道没有可比较候选"的收尾状态。"""
    run = dict(run)
    run['lanes'] = {'price': 'completed', 'alternatives': 'completed'}
    run['alternatives'] = [dict(item) for item in alternatives]
    run['reference_candidates'] = [dict(item) for item in references]
    run['discovered_candidates'] = [dict(item) for item in references]
    run['comparison_basis'] = 'confirmed_parameters'
    run['confirmed_parameter_count'] = 1
    run['gaps'] = []
    service.save(run)
    service._execute(run['task_id'], run['id'])
    return service.get(run['task_id'], run['id'])


def test_a_lead_without_a_brand_is_not_reported_as_a_similarity_shortfall(research):
    service, run, _, _ = research
    updated = _finished_run(service, run, [
        {'brand': '', 'model': 'E10', 'url': 'https://example.edu.cn/e10',
         'configuration': '容量 100ml', 'config_match': {'hard_failures': []},
         'reference_reason': 'brand_unresolved'},
        {'brand': '乙厂', 'model': 'B20', 'url': 'https://example.edu.cn/b20',
         'configuration': '容量 20ml', 'config_match': {'hard_failures': ['容量']},
         'reference_reason': 'config_hard_constraint'},
    ])
    gaps = [str(gap) for gap in (updated.get('gaps') or [])]

    assert any('没有一条进入可比较位' in gap for gap in gaps), gaps
    assert FLAT not in gaps, '一条候选都没算过相似度，不能说"没有达到70%已核验相似度"'


def test_one_lead_that_also_missed_a_hard_constraint_reports_both_facts(research):
    service, run, _, _ = research
    updated = _finished_run(service, run, [
        {'brand': '', 'model': 'E10', 'url': 'https://example.edu.cn/e10',
         'configuration': '容量 100ml', 'config_match': {'hard_failures': ['容量']},
         'reference_reason': 'brand_unresolved'},
    ])
    gaps = ' '.join(str(gap) for gap in (updated.get('gaps') or []))

    # 原因码只显示"品牌没读到"（品牌判在硬约束之前），但配置也不符——两件都要说。
    assert '品牌或供应商' in gaps, gaps
    assert '容量' in gaps, gaps


def test_a_candidate_that_was_verified_and_missed_the_threshold_still_says_so(research):
    service, run, _, _ = research
    updated = _finished_run(service, run, [], alternatives=[
        {'brand': '乙厂', 'model': 'B20', 'url': 'https://example.edu.cn/b20',
         'configuration': '容量 100ml', 'comparison_disposition': 'needs_review',
         'similarity_score': 0.5, 'similarity_counts': {'satisfied': 1, 'violated': 1}},
    ])
    gaps = [str(gap) for gap in (updated.get('gaps') or [])]

    assert any('逐项核验' in gap and '70%' in gap for gap in gaps), gaps


def test_candidates_in_the_list_without_a_recorded_score_keep_the_old_line(research):
    """候选进了名单、矩阵还没建（老记录）时不改口径：那句说的是名单里没有达标的候选。"""
    service, run, _, _ = research
    updated = _finished_run(service, run, [], alternatives=[
        {'brand': '乙厂', 'model': 'B20', 'url': 'https://example.edu.cn/b20',
         'configuration': '容量 100ml'},
    ])
    gaps = [str(gap) for gap in (updated.get('gaps') or [])]

    assert FLAT in gaps, gaps
