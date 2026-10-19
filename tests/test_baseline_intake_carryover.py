# -*- coding: utf-8 -*-
"""A confirmed baseline must carry every condition the interview recorded.

The interview state and the baseline payload arrive from two different inputs.
When the interview already holds a supplier statement (for example
``warranty=12个月``) and the payload leaves the field out, saving it turns a
known condition into "销售尚未说明" with no trace. These tests pin the two
cases apart:

* recorded in the interview, missing from the payload -> refused, and the
  error names the fields so the user can bring them back;
* never recorded, because the supplier said nothing -> saves normally and the
  report keeps reading 销售尚未说明. This is a real answer, not a loss.
"""
import pytest

from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_research import ProcurementResearch, uncovered_intake_fields

# The statement set a complete interview collects. ``requirements`` and
# ``recording`` are folded into the baseline scenario/parameters by
# ``draft_baseline``, so they are not compared field by field.
FULL_FACTS = {
    'category': '生化培养箱', 'brand': '泰斯特', 'model': 'SPX-150BIII',
    'scenario': '微生物培养', 'requirements': '25-42℃', 'recording': '温度记录并导出 CSV',
    'supplier_price': 38000, 'quantity': 1, 'currency': 'CNY',
    'configuration': '标准配置', 'tax': '含税', 'shipping': '含运费',
    'installation': '不含安装培训', 'warranty': '12个月',
    'service_response': '24小时', 'service_onsite': '48小时', 'delivery': '30天',
}
UNSTATED_FIELDS = ('warranty', 'service_response', 'service_onsite', 'delivery')
SCENARIO = '微生物培养；25-42℃；温度记录并导出 CSV'
PARAMETERS = [{'name': '使用温度', 'value': 37, 'unit': '℃', 'direction': 'equal'}]


def _save_interview(repo, facts, revision=6):
    repo.put('procurement_records', 'intake-t', {
        'id': 'intake-t', 'kind': 'intake', 'task_id': 't', 'revision': revision,
        'facts': {key: {'value': value, 'quote': str(value), 'origin': 'user_statement'}
                  for key, value in facts.items()},
        'parameters': [], 'deferred': [], 'messages': [],
    }, task_id='t')


def _interview(tmp_path, facts):
    """A task whose interview already recorded ``facts``."""
    repo = SQLiteRepository(tmp_path / 'interview.db')
    repo.put('tasks', 't', {'id': 't', 'scenario': '微生物培养'})
    _save_interview(repo, facts)
    return repo, ProcurementResearch(repo, None, None)


def _payload(**overrides):
    """What the confirmation form submits, by default carrying everything."""
    payload = {key: value for key, value in FULL_FACTS.items()
               if key not in ('requirements', 'recording')}
    payload.update({'scenario': SCENARIO, 'parameters': list(PARAMETERS), 'intake_revision': 6})
    payload.update(overrides)
    return payload


def test_a_payload_that_drops_interview_conditions_is_refused_not_silently_saved(tmp_path):
    repo, service = _interview(tmp_path, FULL_FACTS)
    with pytest.raises(ValueError) as failure:
        service.confirm('t', _payload(warranty=None, delivery=''), start=False)
    assert '保修期限' in str(failure.value) and '交货时间' in str(failure.value)
    assert not [r for r in repo.list('procurement_records', 't') if r.get('kind') == 'baseline']
    repo.close()


def test_uncovered_intake_fields_names_only_what_the_payload_drops(tmp_path):
    from backend.services.procurement_intake import read_intake
    repo, _ = _interview(tmp_path, FULL_FACTS)
    state = read_intake(repo, 't')
    assert uncovered_intake_fields(state, _payload()) == []
    assert uncovered_intake_fields(state, _payload(quantity=None, tax='')) == ['quantity', 'tax']
    repo.close()


def test_conditions_the_supplier_never_stated_still_save_and_read_as_unstated(tmp_path):
    facts = {key: value for key, value in FULL_FACTS.items() if key not in UNSTATED_FIELDS}
    repo, service = _interview(tmp_path, facts)
    payload = {key: value for key, value in facts.items() if key not in ('requirements', 'recording')}
    payload.update({'scenario': SCENARIO, 'parameters': list(PARAMETERS), 'intake_revision': 6})
    run = service.confirm('t', payload, start=False)
    baseline = run['baseline']
    assert baseline['warranty'] is None and baseline['delivery'] is None
    assert baseline['tax'] == '含税' and baseline['supplier_price'] == 38000
    section = next(item for item in service.report('t', run['id'])['sections'] if item['key'] == 'baseline')
    shown = {item['label']: item['text'] for item in section['items']}
    # 口径（2026-09 修 8c）：空白的销售字段要能分清"我们没问"和"问了、对方说没有"。
    # 这一单里保修与交货**从采集阶段就没有问过**（facts 里就没有），本轮也没有收到任何
    # 销售回复，所以报告如实写"没有向销售询问这一项"，不再用含糊的"销售尚未说明"——
    # 那句话在"销售明确答过资料中未提供"的场合会撒谎。三态各自的判据见
    # ``procurement_research.missing_information_state``。
    assert shown['保修期限'] == '本轮没有向销售询问这一项'
    assert shown['税费'] == '含税'
    repo.close()


def test_a_payload_that_carries_everything_confirms_the_same_values(tmp_path):
    repo, service = _interview(tmp_path, FULL_FACTS)
    baseline = service.confirm('t', _payload(), start=False)['baseline']
    assert baseline['warranty'] == '12个月' and baseline['service_onsite'] == '48小时'
    assert baseline['installation'] == '不含安装培训' and baseline['delivery'] == '30天'
    assert baseline['supplier_price'] == 38000 and baseline['quantity'] == 1
    repo.close()


def test_a_supplier_reply_is_not_blocked_by_an_older_baseline(tmp_path):
    """Repricing an already-confirmed baseline must not re-open the interview."""
    repo, service = _interview(tmp_path, {})
    run = service.confirm('t', _payload(category=None, brand=None, currency=None, supplier_price=None,
                                        quantity=None, configuration=None, tax=None, shipping=None,
                                        installation=None, warranty=None, service_response=None,
                                        service_onsite=None, delivery=None), start=False)
    assert run['baseline']['warranty'] is None
    # The interview records the after-sales terms only after that baseline.
    _save_interview(repo, FULL_FACTS, revision=7)
    updated = service.supplier_update('t', run['id'], {'supplier_price': 36000})
    assert updated['baseline']['supplier_price'] == 36000
    repo.close()
