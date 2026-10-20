# -*- coding: utf-8 -*-
"""金额闸门的环境变量必须配套：只设一半要响亮失败，且不许替它编默认额度。

来历：``LAB_COST_LEDGER``
（开金额闸门）与 ``LAB_COST_INITIAL_CNY``（给初始额度）必须配套。只设前一个时，
``monetary_budget.reserve`` 直接 ``os.environ[...]`` 取键，抛出的 ``KeyError`` 既不是
``ValueError``（``deepseek.py:231`` 只捕它），消息里也只有一个变量名，于是**一路逃逸
成未处理崩溃**。口径与 ``AGENTS.md``「对外口径与兜底」一致：配置错误
不兜底、不静默降级——要的是**点名变量的人话报错**，不是给它编一个 0 元默认值。

本文件覆盖三个变量（``monetary_budget.reserve`` 一带的全部读取路径）：
``LAB_COST_LEDGER`` 默认未设＝闸门关、``LAB_COST_INITIAL_CNY`` 无默认、
``LAB_COST_LIMIT_CNY`` 默认 2.8 且被 ``min(2.8, …)`` 封顶。
"""
import sqlite3

import pytest

from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.infrastructure.monetary_budget import BudgetConfigError, reserve

#: 默认模型在费率表里、且不带付费工具，所以它会一路走到读初始额度那一步。
PAYLOAD = {'model': 'deepseek-flash', 'max_tokens': 8192}


def _spend_rows(ledger):
    with sqlite3.connect(ledger) as db:
        return db.execute('SELECT COUNT(*) FROM spend').fetchone()[0]


def test_missing_initial_amount_fails_loudly_and_names_both_variables(tmp_path, monkeypatch):
    """只设闸门、不设初始额度：必须是带 code 的人话错误，不是逃逸的 KeyError。"""
    ledger = tmp_path / 'cost.sqlite3'
    monkeypatch.setenv('LAB_COST_LEDGER', str(ledger))
    monkeypatch.delenv('LAB_COST_INITIAL_CNY', raising=False)
    monkeypatch.delenv('LAB_COST_LIMIT_CNY', raising=False)

    with pytest.raises(BudgetConfigError) as caught:
        reserve(PAYLOAD)

    exc = caught.value
    assert exc.code == 'monetary_budget_not_configured'
    # 修好之后 deepseek.py 的 ``except ValueError`` 接得住它。
    assert isinstance(exc, ValueError)
    text = str(exc)
    assert 'LAB_COST_INITIAL_CNY' in text, '必须点名缺的是哪个变量'
    assert 'LAB_COST_LEDGER' in text, '必须说清它要和谁配套设'
    # 没有兜底，就没有记账：一行都不许写（口径同 test_cost_rate_table.py）。
    assert _spend_rows(ledger) == 0


def test_missing_initial_amount_is_not_replaced_by_a_default(tmp_path, monkeypatch):
    """反向确认：缺失的额度不许被当成 0 元——消息里要写明"不编默认值"。

    拿 0 元起算的后果是把真实花费算漏，所以宁可拒发：这正是"给配置错误兜底
    等于骗用户"那条口径要挡住的。
    """
    monkeypatch.setenv('LAB_COST_LEDGER', str(tmp_path / 'cost.sqlite3'))
    monkeypatch.delenv('LAB_COST_INITIAL_CNY', raising=False)

    with pytest.raises(BudgetConfigError, match='默认值'):
        reserve(PAYLOAD)


def test_non_numeric_or_blank_amounts_name_the_variable(tmp_path, monkeypatch):
    """设了但不是数字（或设成空）：也要点名变量，不许拿默认值顶上去。"""
    monkeypatch.setenv('LAB_COST_LEDGER', str(tmp_path / 'cost.sqlite3'))
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '2.65')

    monkeypatch.setenv('LAB_COST_LIMIT_CNY', '两块八')
    with pytest.raises(BudgetConfigError, match='LAB_COST_LIMIT_CNY'):
        reserve(PAYLOAD)

    monkeypatch.setenv('LAB_COST_LIMIT_CNY', '')
    with pytest.raises(BudgetConfigError, match='LAB_COST_LIMIT_CNY'):
        reserve(PAYLOAD)

    monkeypatch.delenv('LAB_COST_LIMIT_CNY')
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '两块六五')
    with pytest.raises(BudgetConfigError, match='LAB_COST_INITIAL_CNY'):
        reserve(PAYLOAD)


def test_gate_off_ignores_the_amount_variables(tmp_path, monkeypatch):
    """闸门默认关闭：不设 ``LAB_COST_LEDGER`` 时额度变量一律不生效，也不报错。"""
    monkeypatch.delenv('LAB_COST_LEDGER', raising=False)
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '2.65')
    monkeypatch.setenv('LAB_COST_LIMIT_CNY', '两块八')

    assert reserve(PAYLOAD) is None


def test_client_reports_half_configured_gate_as_a_named_error(tmp_path, monkeypatch):
    """走真实调用路径：配置错误必须变成 DeepSeekError 的 code＋人话，而不是崩溃。

    故意用生产 transport——配置错误在发出任何网络请求之前就该被拦下。
    """
    monkeypatch.setenv('LAB_COST_LEDGER', str(tmp_path / 'cost.sqlite3'))
    monkeypatch.delenv('LAB_COST_INITIAL_CNY', raising=False)

    client = DeepSeekClient('test-key')
    with pytest.raises(DeepSeekError) as caught:
        client.analyze(system_prompt='json', user_prompt='{"ok":true}')

    exc = caught.value
    assert exc.code == 'monetary_budget_not_configured'
    assert 'LAB_COST_INITIAL_CNY' in exc.message
    assert 'LAB_COST_LEDGER' in exc.message
