"""Refusal records belong to a target and must not be overwritten by the next one.

Measured on a frozen acceptance run: 15 refusals were recorded and every single
one belonged to the last alternative candidate (``Avanti J-15R``); not one row
was left for the model the run was actually about (``XKA-2200``), even though the
extractor had returned a price for it. The mechanism is
``procurement_research.prices()`` assigning ``run['price_rejections']`` wholesale
at the end -- and ``alternative_prices()`` calls ``prices()`` once per candidate.
"""
from backend.services.price_rejections import NOT_AWARDED_CANDIDATE
from tests.test_candidate_notice_prices import LOSER_ROW, _source  # noqa: F401
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

TARGET = {'brand': '路迈', 'model': 'LS-1'}
OTHER = {'brand': '别的厂', 'model': 'OTHER-1'}


def _rejections_for(run, model):
    return [item for item in (run.get('price_rejections') or []) if item.get('model') == model]


def test_a_later_target_does_not_erase_an_earlier_targets_rejections(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    run['sources'] = [_source()]
    service.model = lambda run, prompt, data: {'prices': [dict(LOSER_ROW)]}

    assert service.prices(run, TARGET) == []
    recorded = _rejections_for(run, 'LS-1')
    assert recorded, '主型号的拒绝理由必须先被记下来'
    assert recorded[0]['reason_code'] == NOT_AWARDED_CANDIDATE

    # The second call is what an alternative candidate does to this run.
    service.prices(run, OTHER)

    assert _rejections_for(run, 'LS-1'), (
        f'主型号的拒绝记录被后一个目标整体挤掉了：{run.get("price_rejections")}')
    assert len(_rejections_for(run, 'LS-1')) == len(recorded)


def test_re_running_one_target_replaces_that_targets_own_entries(research):  # noqa: F811
    """Keeping other targets must not turn into piling up duplicates of this one."""
    service, run, _, _ = research
    service.verify_publishers = lambda *args: None
    run['sources'] = [_source()]
    service.model = lambda run, prompt, data: {'prices': [dict(LOSER_ROW)]}

    service.prices(run, TARGET)
    first = len(_rejections_for(run, 'LS-1'))
    assert first

    service.prices(run, TARGET)

    assert len(_rejections_for(run, 'LS-1')) == first
