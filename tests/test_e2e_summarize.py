from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[1]
_CANDIDATE_TOOLS = (
    ROOT / 'output' / 'corpus-v2-20261002' / 'tools' / 'summarize.py',
    Path(r'D:\Data\labpilot\corpus-v2-20261002\tools\summarize.py'),
)
TOOL = next((p for p in _CANDIDATE_TOOLS if p.is_file()), _CANDIDATE_TOOLS[0])


def load_tool():
    if not TOOL.is_file():
        pytest.skip(f'外部语料工具不在本机: {TOOL}')
    spec = importlib.util.spec_from_file_location('corpus_v2_summarize', TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_round_retry_does_not_override_a_completed_no_price_lookup():
    tool = load_tool()

    classification = tool.classify(
        {'status': 'no_price_source', 'price_count': 0},
        200,
        [{'step': 'start-alternatives-retries', 'attempts': 2}],
    )

    assert classification == ('② 通路通但确实没这个价', 'detail.status=no_price_source')


def test_non_200_lookup_remains_channel_failure_even_without_round_errors():
    tool = load_tool()

    classification = tool.classify(None, 502, [])

    assert classification == ('① 通道不通', '查价接口返回 HTTP 502')


def test_on_demand_lookup_is_not_reported_as_a_channel_failure():
    tool = load_tool()

    classification = tool.classify(
        {'status': 'not_requested', 'reason_code': 'not_requested', 'price_count': 0},
        200,
        [],
    )

    assert classification == ('按需查价（尚未执行）', '用户尚未点击查价，接口没有发起请求')


def test_no_formal_candidate_is_not_misreported_as_price_channel_failure(tmp_path):
    tool = load_tool()
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    (evidence / '02-upload.json').write_text('{"pages": []}', encoding='utf-8')
    (evidence / '04-analysis-summary.json').write_text('{}', encoding='utf-8')
    (evidence / '09-candidate-pick.json').write_text('{}', encoding='utf-8')
    (evidence / '09b-candidates-before.json').write_text('{}', encoding='utf-8')
    (evidence / '10-price-first.json').write_text('{}', encoding='utf-8')
    (evidence / '11-price-second.json').write_text('{}', encoding='utf-8')
    (evidence / '12-price-404.json').write_text('{}', encoding='utf-8')
    (evidence / '13-price-report.json').write_text('{}', encoding='utf-8')
    (evidence / '16-final-run.json').write_text(
        '{"alternatives": [], "reference_candidates": [{"model": "Q1"}], "sources": []}',
        encoding='utf-8',
    )

    summary = tool.summarize(tmp_path)

    assert summary['no_candidate_note'].startswith('本轮没有正式候选')
    assert summary['price_lookup_class'] == {
        'class': '无正式候选（未执行）',
        'why': '正式候选数为 0，候选品查价未发起',
        'rejection_reasons': None,
        'skip_reasons': None,
        'sources_scanned': None,
        'sources_eligible': None,
        'sources_cost_gated': None,
        'sources_by_channel': None,
    }
