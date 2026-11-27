# -*- coding: utf-8 -*-
"""有产品资料时，比较基准改用资料里识别出的型号；口述型号和它的报价另记。

规则来自业务口径：「如果用户没有提供材料 则按用户提出的型号为准；如果用户提供产品资料
则以产品资料中的型号为准，读取后直接在首页对话框显示给客户看」。

这份测试盯住四件事，缺一件这个功能就会在真实环境里悄悄失效：

* 资料型号真的替换了口述型号，成为比较基准；
* 口述型号与它挂着的报价没被丢掉（``quote_model``），否则会把 A 机器的报价安到 B 头上；
* 资料里出现多个候选时**不**替换，而是在对话里列出来让用户选；
* 每次读 intake 都会重算那条对话说明，但不能越读越多。
"""
import pytest

from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_intake import DOCUMENT_NOTICE, draft_baseline, read_intake, respond
from backend.services.procurement_research import ProcurementResearch
from backend.services.procurement_rules import price_assessment

SPOKEN_MODEL = 'Master-S15'
SPOKEN_BRAND = '上海和泰'
DOCUMENT_MODEL = 'RO DI400'
DOCUMENT_BRAND = '和泰 Hitech'

SPOKEN_FACTS = {
    'category': '实验室超纯水机', 'brand': SPOKEN_BRAND, 'model': SPOKEN_MODEL,
    'scenario': '分子生物学实验室的试剂配制与仪器供水', 'supplier_price': 38000,
    'currency': 'CNY', 'quantity': 1, 'configuration': '配 15L 压力储水桶和独立取水手臂',
}


def _candidate(model, brand, status='pending_review', sources=True):
    candidate = {'candidate_id': 'analysis-' + model.replace(' ', ''), 'brand': brand, 'model': model,
                 'configuration': '厂家标准配置', 'status': status}
    candidate['sources'] = [{'document_id': 'doc-1', 'page_or_location': 'page:1', 'excerpt': model}] if sources else []
    return candidate


def _interview(tmp_path, candidates, facts=None):
    """一条已经被销售回复填过的访谈记录，外加一份资料分析报告。"""
    repo = SQLiteRepository(tmp_path / 'intake.db')
    repo.put('tasks', 't', {'id': 't', 'scenario': '分子生物学实验室的试剂配制与仪器供水', 'category': '实验室超纯水机'})
    recorded = SPOKEN_FACTS if facts is None else facts
    record = {
        'id': 'intake-t', 'kind': 'intake', 'task_id': 't', 'revision': 4,
        'facts': {key: {'value': value, 'quote': str(value), 'origin': 'user_statement'}
                  for key, value in recorded.items()},
        'parameters': [{'name': '产水电阻率', 'value': 18.2, 'unit': 'MΩ·cm', 'direction': 'min', 'conditions': ''}],
        'deferred': [], 'messages': [{'role': 'user', 'content': '销售回复：推荐上海和泰 Master-S15。'}],
        'question': {'group': 'service', 'text': '还缺少报修响应时间。'},
    }
    # 真实记录里的 draft 是 respond() 写下的 draft_baseline 结果，这里照做。
    record['draft'] = draft_baseline(record)
    repo.put('procurement_records', 'intake-t', record, task_id='t')
    if candidates is not None:
        repo.put('reports', 'analysis-1', {'kind': 'deepseek_analysis', 'created_at': '2026-09-24T01:38:13+00:00',
                                           'candidates': candidates}, task_id='t')
    return repo


def _notices(state):
    return [m for m in state['messages'] if m.get('kind') == DOCUMENT_NOTICE]


def _payload(state):
    """确认页提交的东西：预填草稿 + 用户点确认。"""
    draft = dict(state['draft'])
    draft.update({'parameters': state['parameters'] or [{'name': '产水电阻率', 'value': 18.2, 'unit': 'MΩ·cm',
                                                         'direction': 'min', 'conditions': ''}]})
    return draft


# --- 1. 资料型号覆盖口述型号 -------------------------------------------------

def test_a_unique_document_model_becomes_the_comparison_basis(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == DOCUMENT_MODEL
    assert state['draft']['brand'] == DOCUMENT_BRAND
    assert state['draft']['category'] == '实验室超纯水机'   # 资料没给品类，保留原值
    assert state['document_identity']['overrides_spoken'] is True
    assert state['document_suggestions'] == {'brand': DOCUMENT_BRAND, 'model': DOCUMENT_MODEL}
    repo.close()


def test_the_spoken_model_survives_as_a_user_statement_and_keeps_the_quote(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    # 口述的事实一字不改：它仍是用户陈述，不是待确认的推测
    assert state['facts']['model'] == {'value': SPOKEN_MODEL, 'quote': SPOKEN_MODEL, 'origin': 'user_statement'}
    assert state['facts']['brand']['value'] == SPOKEN_BRAND
    # 报价挂在口述型号上，不跟着比较基准搬走
    assert state['draft']['supplier_price'] == 38000.0
    assert state['draft']['quote_model'] == SPOKEN_MODEL
    assert state['draft']['quote_brand'] == SPOKEN_BRAND
    repo.close()


def test_the_document_model_never_becomes_a_user_fact(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    assert DOCUMENT_MODEL not in [entry['value'] for entry in state['facts'].values()]
    # 而且读一次 intake 不能把资料型号写进库里
    assert repo.get('procurement_records', 'intake-t')['draft'].get('model') == SPOKEN_MODEL
    repo.close()


def test_confirming_the_draft_keeps_the_quote_attached_to_the_spoken_model(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    service = ProcurementResearch(repo, None, None)
    state = read_intake(repo, 't')
    run = service.confirm('t', _payload(state), start=False)
    baseline = run['baseline']
    assert baseline['model'] == DOCUMENT_MODEL and baseline['brand'] == DOCUMENT_BRAND
    assert baseline['quote_model'] == SPOKEN_MODEL and baseline['quote_brand'] == SPOKEN_BRAND
    assert baseline['supplier_price'] == 38000
    repo.close()


def test_the_quote_is_never_compared_as_if_it_were_the_document_model(tmp_path):
    """报价 38000 是 Master-S15 的；资料基准是 RO DI400，两者不能混着比。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    service = ProcurementResearch(repo, None, None)
    state = read_intake(repo, 't')
    run = service.confirm('t', _payload(state), start=False)
    baseline = run['baseline']
    summary = price_assessment([{'kind': 'award', 'unit_price': 30000, 'level': 'historical_configuration',
                                 'gaps': [], 'url': 'https://buyer.gov.cn/1'}], baseline)
    assert SPOKEN_MODEL in summary and DOCUMENT_MODEL in summary
    assert '同型号' not in summary
    section = next(item for item in service.report('t', run['id'])['sections'] if item['key'] == 'baseline')
    shown = {item['label']: item['text'] for item in section['items']}
    assert shown['销售单价'].startswith('38000')
    assert SPOKEN_MODEL in shown['销售单价'] and DOCUMENT_MODEL in shown['销售单价']
    assert shown['比较基准（产品资料）'].startswith(DOCUMENT_BRAND)
    assert SPOKEN_MODEL in shown['销售推荐'] and DOCUMENT_MODEL not in shown['销售推荐']
    repo.close()


def test_the_report_headline_does_not_call_the_quote_same_model(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    service = ProcurementResearch(repo, None, None)
    state = read_intake(repo, 't')
    run = service.confirm('t', _payload(state), start=False)
    headline = service.report('t', run['id'])['summary']['headline']
    assert SPOKEN_MODEL in headline and DOCUMENT_MODEL in headline
    assert '同型号成交记录' not in headline
    repo.close()


# --- 2. 多候选不覆盖 --------------------------------------------------------

def test_two_document_candidates_are_listed_instead_of_overriding(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND),
                                 _candidate('Smart-S15', SPOKEN_BRAND)])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == SPOKEN_MODEL          # 比较基准没被替换
    assert 'document_suggestions' not in state
    assert [option['model'] for option in state['document_candidates']] == [DOCUMENT_MODEL, 'Smart-S15']
    notice = _notices(state)[0]['content']
    assert DOCUMENT_MODEL in notice and 'Smart-S15' in notice and '没有替换' in notice
    repo.close()


def test_a_conflicted_candidate_asks_instead_of_overriding(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND),
                                 _candidate('RO DI800', DOCUMENT_BRAND, status='conflicted')])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == SPOKEN_MODEL
    assert [option['model'] for option in state['document_candidates']] == [DOCUMENT_MODEL, 'RO DI800']
    repo.close()


def test_a_product_the_user_already_rejected_is_not_offered_again(tmp_path):
    """rejected 是用户自己排除过的产品，不是冲突；否则每次读都要再问一遍，用户没法退出。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND),
                                 _candidate('RO DI800', DOCUMENT_BRAND, status='rejected')])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == DOCUMENT_MODEL
    assert not _notices(state)[0]['content'].count('多个候选型号')
    repo.close()


def test_a_conflicted_only_report_overrides_nothing(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND, status='conflicted')])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == SPOKEN_MODEL
    assert state['draft'].get('quote_model') is None
    repo.close()


def test_a_named_model_narrows_multiple_candidates_to_one(tmp_path):
    """资料里有多台、用户自己点过名时收敛到那一台，不再把全部候选摆出来让他挑。"""
    repo = _interview(tmp_path, [_candidate(SPOKEN_MODEL, SPOKEN_BRAND),
                                 _candidate('Smart-S15', SPOKEN_BRAND)])
    for _ in range(2):        # 每次读 intake 都重算这条说明，但只能有一条
        state = read_intake(repo, 't')
        assert len(_notices(state)) == 1
    assert state['document_identity']['model'] == SPOKEN_MODEL
    assert state['document_identity']['converged'] is True
    assert 'document_candidates' not in state
    notice = _notices(state)[0]['content']
    assert SPOKEN_MODEL in notice and 'Smart-S15' not in notice
    assert '就是你提到的型号' in notice and '无法确定哪一台' not in notice
    # 收敛到的是同一台机器，报价不该被当成另一台的
    assert state['draft'].get('quote_model') is None and state['draft']['supplier_price'] == 38000.0
    repo.close()


def test_a_spoken_model_with_the_brand_prefix_is_not_a_second_machine(tmp_path):
    """口述写成 ``上海和泰Master-S15``、资料只写型号时，是同一台，不能判成两台去搬报价。"""
    facts = {**SPOKEN_FACTS, 'model': SPOKEN_BRAND + SPOKEN_MODEL}
    repo = _interview(tmp_path, [_candidate(SPOKEN_MODEL, SPOKEN_BRAND),
                                 _candidate('Smart-S15', SPOKEN_BRAND)], facts=facts)
    state = read_intake(repo, 't')
    assert state['document_identity']['model'] == SPOKEN_MODEL
    assert state['document_identity']['overrides_spoken'] is False
    assert '不是同一台' not in _notices(state)[0]['content']
    repo.close()


def test_a_named_model_that_matches_nothing_still_lists_every_candidate(tmp_path):
    """一个都命中不了时照旧让用户选：资料里的型号一台都不能被顶掉，也不能自己挑一台。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND),
                                 _candidate('RO DI800', DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    assert 'document_identity' not in state
    assert [option['model'] for option in state['document_candidates']] == [DOCUMENT_MODEL, 'RO DI800']
    assert state['draft']['model'] == SPOKEN_MODEL
    assert '无法确定哪一台' in _notices(state)[0]['content']
    repo.close()


def test_a_named_model_matching_two_candidates_still_asks(tmp_path):
    """口述型号同时命中资料里的两台（同型号两种写法各算一台）时不猜，仍然问用户。"""
    facts = {**SPOKEN_FACTS, 'model': SPOKEN_BRAND + SPOKEN_MODEL}
    repo = _interview(tmp_path, [_candidate(SPOKEN_MODEL, SPOKEN_BRAND),
                                 _candidate(SPOKEN_BRAND + SPOKEN_MODEL, SPOKEN_BRAND)], facts=facts)
    state = read_intake(repo, 't')
    assert 'document_identity' not in state
    assert len(state['document_candidates']) == 2
    repo.close()


# --- 3. 没有资料时行为不变 --------------------------------------------------

def test_without_a_document_the_spoken_model_stays_the_basis(tmp_path):
    repo = _interview(tmp_path, None)
    state = read_intake(repo, 't')
    assert state['draft']['model'] == SPOKEN_MODEL and state['draft']['brand'] == SPOKEN_BRAND
    assert 'document_identity' not in state and 'document_suggestions' not in state
    assert state['messages'] == [{'role': 'user', 'content': '销售回复：推荐上海和泰 Master-S15。'}]
    repo.close()


def test_a_document_without_a_model_changes_nothing(tmp_path):
    repo = _interview(tmp_path, [{'candidate_id': 'a', 'brand': DOCUMENT_BRAND, 'model': None,
                                  'status': 'pending_review',
                                  'sources': [{'document_id': 'doc-1', 'excerpt': 'x'}]}])
    state = read_intake(repo, 't')
    assert state['draft']['model'] == SPOKEN_MODEL
    assert not _notices(state)
    repo.close()


def test_a_document_that_only_fills_a_gap_keeps_the_old_behaviour(tmp_path):
    facts = {key: value for key, value in SPOKEN_FACTS.items() if key not in ('brand', 'model')}
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)], facts=facts)
    state = read_intake(repo, 't')
    assert state['draft']['model'] == DOCUMENT_MODEL
    assert 'quote_model' not in state['draft']            # 没有口述型号可挂，不编一个出来
    assert 'model' not in state['facts'] and 'brand' not in state['facts']
    assert DOCUMENT_MODEL not in [entry['value'] for entry in state['facts'].values()]
    assert '已按产品资料为准预填为比较基准' in _notices(state)[0]['content']
    repo.close()


# --- 4. 对话说明只出现一次 --------------------------------------------------

def test_reading_the_intake_repeatedly_does_not_repeat_the_notice(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    for _ in range(3):
        state = read_intake(repo, 't')
        assert len(_notices(state)) == 1
        assert len([m for m in state['messages'] if m['role'] == 'user']) == 1
    repo.close()


def test_a_saved_state_does_not_grow_a_second_notice(tmp_path):
    """``respond`` 会把带上说明的状态写回库里；再读一次必须还是一条。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    repo.put('procurement_records', 'intake-t', state, task_id='t')
    again = read_intake(repo, 't')
    assert len(_notices(again)) == 1
    assert again['draft']['model'] == DOCUMENT_MODEL
    repo.close()


def test_a_new_analysis_replaces_the_old_notice_instead_of_adding_one(tmp_path):
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    repo.put('procurement_records', 'intake-t', read_intake(repo, 't'), task_id='t')
    repo.put('reports', 'analysis-2', {'kind': 'deepseek_analysis', 'created_at': '2026-09-25T00:00:00+00:00',
                                       'candidates': [_candidate('RO DI800', DOCUMENT_BRAND)]}, task_id='t')
    state = read_intake(repo, 't')
    notices = _notices(state)
    assert len(notices) == 1 and 'RO DI800' in notices[0]['content']
    assert state['draft']['model'] == 'RO DI800'
    repo.close()


class _NoopClient:
    def analyze(self, **kwargs):
        return {}


def test_a_new_reply_comes_back_with_the_document_basis_still_prefilled(tmp_path):
    """回复返回的草稿必须是带资料型号的那一份，否则刚发一条消息预填就退回口述型号。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    state = read_intake(repo, 't')
    updated = respond(repo, 't', {'revision': state['revision'], 'message': '好的，就按资料这个型号。'},
                      lambda: _NoopClient())
    assert updated['draft']['model'] == DOCUMENT_MODEL
    assert updated['draft']['quote_model'] == SPOKEN_MODEL
    assert len(_notices(updated)) == 1
    again = read_intake(repo, 't')
    assert len(_notices(again)) == 1
    assert again['draft']['model'] == DOCUMENT_MODEL
    repo.close()


# --- 5. 界面靠的接口字段 ----------------------------------------------------

def test_a_changed_spoken_model_moves_the_quote_attribution_on_the_older_baseline(tmp_path):
    """资料型号没变、销售改口换了型号时，报价的归属必须跟着改，不能留在上一轮的型号上。"""
    repo = _interview(tmp_path, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)])
    service = ProcurementResearch(repo, None, None)
    run = service.confirm('t', _payload(read_intake(repo, 't')), start=False)
    assert run['baseline']['quote_model'] == SPOKEN_MODEL
    record = repo.get('procurement_records', 'intake-t')
    record['revision'] = 5
    record['facts']['model'] = {'value': 'Master-S20', 'quote': 'Master-S20', 'origin': 'user_statement'}
    record['draft'] = draft_baseline(record)
    repo.put('procurement_records', 'intake-t', record, task_id='t')
    state = read_intake(repo, 't')
    assert state['draft']['quote_model'] == 'Master-S20'
    updated = service.confirm('t', _payload(state), start=False)
    assert updated['baseline']['model'] == DOCUMENT_MODEL
    assert updated['baseline']['quote_model'] == 'Master-S20'
    repo.close()


def test_the_baseline_endpoint_marks_a_confirmed_basis_that_the_document_contradicts(tmp_path):
    repo = _interview(tmp_path, None)
    service = ProcurementResearch(repo, None, None)
    service.confirm('t', _payload(read_intake(repo, 't')), start=False)      # 先按口述型号确认
    repo.put('reports', 'analysis-1', {'kind': 'deepseek_analysis', 'created_at': '2026-09-24T01:38:13+00:00',
                                       'candidates': [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)]}, task_id='t')
    view = service.baseline('t')
    assert view['baseline']['model'] == SPOKEN_MODEL          # 已经确认过的记录不被偷偷改写
    assert view['baseline_stale_by_document'] is True
    assert view['document_identity']['model'] == DOCUMENT_MODEL
    assert view['intake_draft']['model'] == DOCUMENT_MODEL
    repo.close()


@pytest.mark.parametrize('candidates', [None, [_candidate(DOCUMENT_MODEL, DOCUMENT_BRAND)]])
def test_the_baseline_endpoint_is_not_stale_without_a_conflict(tmp_path, candidates):
    repo = _interview(tmp_path, candidates)
    service = ProcurementResearch(repo, None, None)
    service.confirm('t', _payload(read_intake(repo, 't')), start=False)
    view = service.baseline('t')
    assert view['baseline_stale_by_document'] is False
    assert view['baseline']['model'] in {SPOKEN_MODEL, DOCUMENT_MODEL}
    repo.close()
