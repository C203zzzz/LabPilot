# -*- coding: utf-8 -*-
"""脚手架不许替用户换基准型号。

批3 实测（2026-10-03）：样本指定 BJ-1CD，产品只识别出 BJ-2CD/BJ-3CD，``pick_baseline``
静默退回第一条，``05-baseline-save.json`` 里的基线成了 BJ-2CD——等于用另一台机器的
链路给出"7/7 通过"。匹配不上必须停下来报错。
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.e2e

ROOT = pathlib.Path(__file__).resolve().parents[1]
_DRIVE_SCRIPT = ROOT / 'output' / 'e2e-final' / 'scripts' / 'drive.py'
if not _DRIVE_SCRIPT.is_file():
    pytest.skip(f'外部验收脚手架不在当前工作区: {_DRIVE_SCRIPT}', allow_module_level=True)
_SPEC = importlib.util.spec_from_file_location('e2e_drive', _DRIVE_SCRIPT)
drive = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drive)


def test_pick_baseline_returns_the_named_candidate():
    candidates = [{'model': 'BJ-2CD'}, {'model': 'BJ-1CD'}]
    assert drive.pick_baseline(candidates, 'BJ-1CD')['model'] == 'BJ-1CD'


def test_pick_baseline_matches_ignoring_case_and_separators():
    candidates = [{'model': 'bj 1cd'}]
    assert drive.pick_baseline(candidates, 'BJ-1CD')['model'] == 'bj 1cd'


def test_pick_baseline_stops_instead_of_switching_to_another_model():
    candidates = [{'model': 'BJ-2CD'}, {'model': 'BJ-3CD'}]
    with pytest.raises(RuntimeError) as error:
        drive.pick_baseline(candidates, 'BJ-1CD')
    message = str(error.value)
    assert 'BJ-1CD' in message and 'BJ-2CD' in message


def test_pick_baseline_falls_back_only_when_no_model_was_named():
    assert drive.pick_baseline([{'model': 'BJ-2CD'}], '')['model'] == 'BJ-2CD'
    assert drive.pick_baseline([{'model': 'BJ-2CD'}], None)['model'] == 'BJ-2CD'


def test_pick_baseline_without_candidates_stays_empty():
    assert drive.pick_baseline([], 'BJ-1CD') == {}
