# -*- coding: utf-8 -*-
"""Candidates must answer the model the user asked about.

The inventory prompt deliberately merges ordering configurations of one
product, and a brochure that lists several models arrives as one list. The user
typed one model, so the stored inventory has to be scoped to it -- but scoping
must never be able to empty the report silently:

* several configurations of the confirmed model stay as separate
  ``pending_review`` candidates (choosing between them is the user's call);
* when nothing matches, the full list stays and the mismatch is recorded as a
  gap, which also flips the report to ``partial``;
* a partial model number is not a match.
"""
import re

import pytest

from backend.infrastructure.deepseek import DeepSeekError
from backend.repositories.sqlite import SQLiteRepository
from backend.services.deepseek_analysis import CANDIDATE_INVENTORY_PROMPT, DeepSeekAnalysisService
from backend.services.procurement_research import confirmed_model

DOCUMENTS = [{'id': 'd', 'pages': [{'number': 1, 'source_location': 'page:1',
                                    'text': 'Model Q10\nModel Q20\nModel X100\n'}]}]


def _service(models):
    """A client that returns one inventory row per model, then echoes details."""
    class Model:
        def analyze(self, system_prompt, user_prompt):
            if system_prompt == CANDIDATE_INVENTORY_PROMPT:
                return {'candidates': [{'model': model,
                                        'sources': [{'document_id': 'd', 'page_or_location': 'page:1',
                                                     'excerpt': 'Model ' + model}]}
                                       for model in models]}
            match = re.search(r'"model":"([^"]+)"', user_prompt)
            return {'candidates': [{'model': match.group(1) if match else models[0], 'fields': []}]}
    return DeepSeekAnalysisService(Model())


def test_analysis_keeps_only_candidates_naming_the_confirmed_model():
    result = _service(['Q10', 'Q20', 'X100']).analyze(scenario='微生物检测', documents=DOCUMENTS, target_model='Q10')
    assert [c['model'] for c in result['candidates']] == ['Q10']
    assert all(c['status'] == 'pending_review' for c in result['candidates'])


def test_analysis_without_a_confirmed_model_keeps_every_candidate():
    result = _service(['Q10', 'Q20', 'X100']).analyze(scenario='微生物检测', documents=DOCUMENTS)
    assert sorted(c['model'] for c in result['candidates']) == ['Q10', 'Q20', 'X100']


def test_a_confirmed_model_matching_nothing_keeps_the_list_and_says_so():
    result = _service(['Q10', 'Q20']).analyze(scenario='微生物检测', documents=DOCUMENTS, target_model='Z9')
    assert sorted(c['model'] for c in result['candidates']) == ['Q10', 'Q20']
    assert any('Z9' in gap for gap in result['gaps'])
    assert result['status'] == 'partial'          # the mismatch is visible, not silent


def test_a_partial_model_number_does_not_match():
    """Q1 must not select Q10 -- fail-closed model identity, not substring."""
    result = _service(['Q10']).analyze(scenario='微生物检测', documents=DOCUMENTS, target_model='Q1')
    assert [c['model'] for c in result['candidates']] == ['Q10']
    assert any('Q1' in gap for gap in result['gaps'])


def test_configurations_of_one_model_all_stay_for_the_user_to_choose():
    result = {'candidates': [{'model': 'Q10', 'configuration': '100 ml'},
                             {'model': 'Q10', 'configuration': '200 ml'}], 'gaps': []}
    DeepSeekAnalysisService._scope_to_target_model(result, 'Q10')
    assert [c['configuration'] for c in result['candidates']] == ['100 ml', '200 ml']
    assert any('请人工确认' in gap for gap in result['gaps'])


def test_a_blank_target_model_never_filters():
    result = {'candidates': [{'model': 'Q10'}], 'gaps': []}
    DeepSeekAnalysisService._scope_to_target_model(result, '   ')
    assert [c['model'] for c in result['candidates']] == ['Q10'] and result['gaps'] == []


def test_confirmed_model_reads_the_newest_baseline_and_nothing_else(tmp_path):
    repo = SQLiteRepository(tmp_path / 'baseline.db')
    assert confirmed_model(repo, 't1') is None
    repo.put('procurement_records', 'b1', {'id': 'b1', 'kind': 'baseline', 'task_id': 't1',
                                           'model': 'Q10', 'created_at': '2026-01-01T00:00:00+00:00'}, task_id='t1')
    assert confirmed_model(repo, 't1') == 'Q10'
    # A later confirmation replaces the earlier one ...
    repo.put('procurement_records', 'b2', {'id': 'b2', 'kind': 'baseline', 'task_id': 't1',
                                           'model': 'Q20', 'created_at': '2026-02-01T00:00:00+00:00'}, task_id='t1')
    assert confirmed_model(repo, 't1') == 'Q20'
    # ... and a record without a model does not erase the last one that had it.
    repo.put('procurement_records', 'b3', {'id': 'b3', 'kind': 'baseline', 'task_id': 't1',
                                           'model': '', 'created_at': '2026-03-01T00:00:00+00:00'}, task_id='t1')
    assert confirmed_model(repo, 't1') == 'Q20'
    # Other tasks and other record kinds are not consulted.
    repo.put('procurement_records', 'r1', {'id': 'r1', 'kind': 'research', 'task_id': 't2',
                                           'model': 'Z9', 'created_at': '2026-04-01T00:00:00+00:00'}, task_id='t2')
    assert confirmed_model(repo, 't2') is None
    repo.close()
