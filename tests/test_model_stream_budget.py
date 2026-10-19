import io
import json
import sqlite3
import pytest
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError, read_event_stream
from backend.infrastructure.monetary_budget import reserve, settle


def stream(events, done=True):
    return io.BytesIO(('\n'.join('data: ' + json.dumps(e) for e in events) + ('\ndata: [DONE]\n' if done else '\n')).encode())


def test_reasoning_is_not_answer_and_usage_only_chunk_is_kept():
    result = read_event_stream(stream([
        {'choices': [{'delta': {'reasoning_content': 'not JSON'}}]},
        {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 5}},
    ]))
    assert result['choices'][0]['message']['content'] == '{"ok":true}'
    assert result['usage']['completion_tokens'] == 5


@pytest.mark.parametrize('events,done', [
    ([{'choices': [{'delta': {'content': '{}'}, 'finish_reason': 'stop'}]}], False),
    ([{'choices': [{'delta': {'content': '{}'}}]}], True),
])
def test_interrupted_stream_cannot_commit_complete_looking_json(events, done):
    with pytest.raises(DeepSeekError, match='传输中断'):
        read_event_stream(stream(events, done))


def test_gateway_capabilities_do_not_mix_vendor_budgets():
    flash = DeepSeekClient('test', capability='qianwen', model='deepseek-v4.1-flash', thinking_enabled=True)
    qwen = DeepSeekClient('test', capability='qianwen', model='qwen3.8-max-0902', thinking_enabled=True)
    assert 'thinking_budget' not in flash.model_options()
    assert flash.model_options()['reasoning_effort'] == 'high'
    assert 'reasoning_effort' not in qwen.model_options()
    assert qwen.model_options()['thinking_budget'] == 2048


def test_length_retry_does_not_cache_partial_answer():
    calls = []
    def transport(*args):
        calls.append(1)
        return {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'length' if len(calls) == 1 else 'stop'}]}
    client = DeepSeekClient('test-retry', transport=transport, json_retries=1)
    assert client.analyze(system_prompt='json', user_prompt='bounded') == {'ok': True}
    assert len(calls) == 2


def test_failed_reservation_survives_restart_and_usage_releases_only_known_cost(tmp_path, monkeypatch):
    ledger = tmp_path / 'cost.sqlite3'
    monkeypatch.setenv('LAB_COST_LEDGER', str(ledger))
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '2.65')
    payload = {'model': 'deepseek-v4.1-flash', 'max_tokens': 8192}
    ticket = reserve(payload)
    with pytest.raises(ValueError, match='上限'):
        reserve(payload)
    settle(ticket, {'prompt_tokens': 10, 'completion_tokens': 20})
    assert reserve(payload)
    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT COUNT(*) FROM spend').fetchone()[0] == 2
