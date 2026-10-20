# -*- coding: utf-8 -*-
"""额度（用量上限）这件事：三处说同一句话，而且事先看得见。

盲测单实测（只读一份已知样本产物）：额度上限写死
20 次/小时、整台部署共享；一份需要逐页视觉分析的 PDF 就要约 30 次，一份普通文本
PDF 约 11–18 次。撞上限时三个出口各说各的话——研究车道说"本轮分析因服务额度限制
中断"（读起来像故障），问答接口说"本小时模型调用已达上限，请稍后再试"（最清楚），
需求访谈那句**完全没提额度**（用户不知道自己被限额挡住）。而且撞上之前毫无预兆。

**默认值已从 20 提到 200**（「先保功能、后收紧成本」）：上面那段"写死 20"是当时的
实测背景，不是现在的行为。这里钉三件事：三个出口口径统一、用量事先可见、出厂默认值
本身（``test_limit_default_is_two_hundred``）。
"""
import re
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.infrastructure.deepseek import (BUDGET_EXCEEDED_MESSAGE, BUDGET_LOW_MESSAGE,
                                             DeepSeekClient, DeepSeekError,
                                             model_call_budget, model_call_limit)
from backend.infrastructure.usage_ledger import reserve_call, usage
from backend.main import create_app
from backend.services.official_search import FakeSearchProvider, OfficialSearchService

REPO = Path(__file__).resolve().parents[1]
RESEARCH_COPY = REPO / 'frontend' / 'src' / 'services' / 'researchCopy.js'
PROCUREMENT_API = REPO / 'backend' / 'api' / 'procurement.py'

#: 这三件事缺一不可，三处都得说到。
REQUIRED = ('用量上限', '试', '结果')


def test_frontend_and_backend_say_exactly_the_same_sentence():
    """前端那句必须与后端常数逐字相同——三处各写一句就会慢慢走样。"""
    source = RESEARCH_COPY.read_text(encoding='utf-8')
    match = re.search(r"BUDGET_EXCEEDED_MESSAGE = '([^']*)'", source)
    assert match, 'researchCopy.js 里找不到统一文案常数'
    assert match.group(1) == BUDGET_EXCEEDED_MESSAGE


@pytest.mark.parametrize('text', [BUDGET_EXCEEDED_MESSAGE, BUDGET_LOW_MESSAGE.format(used=19, limit=20)])
def test_the_sentence_says_the_three_things(text):
    assert '上限' in text, '要说清这是用量上限，不是故障'
    for word in REQUIRED:
        assert word in text, f'这句话少了「{word}」'


def test_the_three_exits_use_the_one_sentence(monkeypatch, tmp_path):
    """三个出口：模型闸门、需求访谈接口、研究车道（前端文案已逐字比对）。"""
    monkeypatch.setenv('LAB_MODEL_CALLS_PER_HOUR', '1')
    calls = []

    def transport(*args):
        calls.append(args)
        return {'choices': [{'message': {'content': '{"ok": true}'}}]}

    client = DeepSeekClient(api_key=secrets.token_hex(12), transport=transport)
    client.analyze(system_prompt='json', user_prompt='first')
    with pytest.raises(DeepSeekError) as raised:
        client.analyze(system_prompt='json', user_prompt='second')
    assert raised.value.code == 'provider_budget_exceeded'
    assert raised.value.message == BUDGET_EXCEEDED_MESSAGE

    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(FakeSearchProvider()))
    with TestClient(app) as http:
        task = http.post('/api/v1/tasks', json={'scenario': '采购酶标仪'}).json()['task']['id']

        # 需求访谈那一路：过去这一句完全没提额度。
        from backend.services import procurement_intake
        monkeypatch.setattr(procurement_intake, 'respond',
                            lambda *a, **k: (_ for _ in ()).throw(
                                DeepSeekError('provider_budget_exceeded', BUDGET_EXCEEDED_MESSAGE)))
        answer = http.post(f'/api/v1/tasks/{task}/intake', json={'message': '预算三十万'})
    assert answer.status_code == 429
    detail = answer.json()['detail']
    assert BUDGET_EXCEEDED_MESSAGE in detail
    assert '尚未保存' in detail, '访谈这条路要先说清本次回答没保存'
    # 非额度类错误仍旧走原来那句，别把两件事混成一句。
    assert '需求整理服务暂不可用' not in detail


def test_the_quota_message_reaches_the_user_through_the_ledger(monkeypatch, tmp_path):
    """闸门那句与 ``model_call_budget`` 报的"还剩 0"必须是同一个事实。"""
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'usage.sqlite3'))
    monkeypatch.setenv('LAB_MODEL_CALLS_PER_HOUR', '20')
    monkeypatch.setattr('backend.infrastructure.deepseek._attempts', {})

    empty = model_call_budget()
    assert (empty['used'], empty['limit'], empty['remaining']) == (0, 20, 20)
    assert empty['notice'] == '', '额度充足时不该打扰用户'
    assert empty['exhausted_message'] == BUDGET_EXCEEDED_MESSAGE

    for _ in range(16):
        reserve_call('https://api.deepseek.com/chat/completions', 20)
    low = model_call_budget()
    assert (low['used'], low['remaining']) == (16, 4)
    assert low['notice'], '只剩 4 次（低于 5 次水线）时必须给出提醒句'
    assert '稍后再试' in low['notice']
    assert 0 < low['reset_in_seconds'] <= 3600

    for _ in range(4):
        reserve_call('https://api.deepseek.com/chat/completions', 20)
    spent = model_call_budget()
    assert (spent['used'], spent['remaining']) == (20, 0)
    assert spent['notice']


def test_limit_default_is_two_hundred(monkeypatch):
    """出厂默认值就是这一条断言的全部内容——它变了，说明成本决策变了。

    20 是 2026-09-28 之前的旧值（真跑里会把用户挡在门外：一份扫描件约 30 次、
    而额度整台部署共享）。按「先保功能、后收紧成本」定下后，
    ``backend/infrastructure/deepseek.py`` 的默认值提到 **200**。收紧时改回来要
    **连这里一起改**，不许只改代码让这条断言变红再删掉它。
    """
    monkeypatch.delenv('LAB_MODEL_CALLS_PER_HOUR', raising=False)
    assert model_call_limit() == 200


def test_budget_endpoint_is_read_only_and_needs_no_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'missing.sqlite3'))
    monkeypatch.delenv('LAB_MODEL_CALLS_PER_HOUR', raising=False)
    # 进程内计数是全局的：别的用例（包括真跑过的研究）会往里写，这里只看账本这一路。
    monkeypatch.setattr('backend.infrastructure.deepseek._attempts', {})
    app = create_app(database_path=tmp_path / 'app.db', storage_dir=tmp_path / 'files',
                     search_service=OfficialSearchService(FakeSearchProvider()))
    with TestClient(app) as http:
        body = http.get('/api/v1/runtime/model-budget')
    assert body.status_code == 200
    payload = body.json()
    assert payload['limit'] == 200 and payload['used'] == 0 and payload['remaining'] == 200
    assert payload['window_seconds'] == 3600
    assert 'notice' in payload and 'exhausted_message' in payload
    assert not (tmp_path / 'missing.sqlite3').exists(), '只读接口不该顺手把账本建出来'


def test_ledger_usage_counts_and_reports_reset(monkeypatch, tmp_path):
    monkeypatch.setenv('LAB_USAGE_DATABASE', str(tmp_path / 'usage.sqlite3'))
    assert usage('provider', 5)['used'] == 0
    reserve_call('provider', 5)
    reported = usage('provider', 5)
    assert (reported['used'], reported['remaining'], reported['limit']) == (1, 4, 5)
    assert 0 < reported['reset_in_seconds'] <= 3600
    assert usage('another-provider', 5)['used'] == 0, '额度按 provider 各算各的'


def test_the_intake_route_sources_its_budget_sentence_from_the_constant():
    """防回归：抄一句新的中文进 ``procurement.py`` 就等于又分叉了。"""
    source = PROCUREMENT_API.read_text(encoding='utf-8')
    assert 'BUDGET_EXCEEDED_MESSAGE' in source
    assert '本轮分析因服务额度限制中断' not in source
