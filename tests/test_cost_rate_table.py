# -*- coding: utf-8 -*-
"""费率表覆盖：缺项必须响亮失败，不许静默算 0、也不许拿别的模型的费率顶上。

来历（未决盘点 ``output/open-items/report.md`` 的 E3、``设计规范``）：默认模型
``deepseek-flash`` 不在 ``monetary_budget.RATES`` 里 —— 一设 ``LAB_COST_LEDGER``
就"费用计划未覆盖此模型"直接拒发（``设计规范``、``设计规范``、``设计规范``），
而外部的成本报告只能自己猜费率：``output/e2e-final/scripts/cost.py`` 当时是
"挑第一个带 deepseek 的键顶上"，于是 ``deepseek-flash`` 的真实调用被按
``deepseek-v4.1-flash`` 的键折算（``output/e2e-final/runs/sample-a-rerun2/cost.json``
里 ``rate.model_key`` 与 ``metered.by_model`` 对不上）。

费率来源（不许凭记忆）：官方价目表
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ （2026-10-01 取）写明
模型名 ``deepseek-flash`` 的模型版本就是 DeepSeek-V4.1-Flash，输入（缓存未命中）
高峰 2 元/百万、输出高峰 8 元/百万，空闲时段减半（1 / 4）。本地交叉核对：
``设计规范`` 与 ``output/e2e-final/runs/*/cost.json`` 里真实外发的模型串是
``deepseek-flash``，金额 (=2×输入 + 8×输出) 与账本实测 tokens 逐条对得上。
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from backend.infrastructure import monetary_budget
from backend.infrastructure.deepseek import DeepSeekClient
from backend.infrastructure.monetary_budget import RATES, rate_for, reserve

ROOT = Path(__file__).resolve().parents[1]
RATE_SOURCE = ROOT / 'backend' / 'infrastructure' / 'monetary_budget.py'
COST_SCRIPT = ROOT / 'output' / 'e2e-final' / 'scripts' / 'cost.py'

#: 官方价目表上的高峰时段价（元/百万 token，输入缓存未命中 / 输出）。
OFFICIAL_FLASH_PEAK = (2, 8)


def _cost_module():
    """按路径加载端到端脚手架里的成本折算脚本（它不在包内，故意不 import backend）。"""
    spec = importlib.util.spec_from_file_location('e2e_final_cost', COST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_model_is_priced():
    """产品默认外发的模型名必须在费率表里：不在，账本一开就整条通道拒发。"""
    assert DeepSeekClient('test-key').model in RATES
    assert rate_for(DeepSeekClient('test-key').model) == OFFICIAL_FLASH_PEAK


def test_flash_rate_matches_official_price_page():
    assert rate_for('deepseek-flash') == OFFICIAL_FLASH_PEAK
    # 网关上的写法是同一个模型版本，同价；两个键都在，换个入口不会再缺项。
    assert rate_for('deepseek-v4.1-flash') == OFFICIAL_FLASH_PEAK
    assert rate_for(' DeepSeek-Flash ') == OFFICIAL_FLASH_PEAK


def test_unpriced_model_is_none_never_zero():
    """查不到费率只有一个语义：算不出来。返回 0 就是把未知冒充成已知金额。"""
    assert rate_for('deepseek-chat') is None
    assert rate_for('') is None
    assert rate_for(None) is None


def test_missing_rate_fails_loudly_and_writes_no_row(tmp_path, monkeypatch):
    """红确认：把 ``deepseek-flash`` 从费率表里抠掉 —— 必须拒发且账本一行不写。

    修复前这里只会说"当前费用计划未覆盖此模型或付费工具"，既不点模型名，也把
    "费率缺项"和"付费工具未覆盖"混成一句；更要紧的是没有任何地方能证明它**没有**
    按 0 元记一笔。
    """
    ledger = tmp_path / 'cost.sqlite3'
    monkeypatch.setenv('LAB_COST_LEDGER', str(ledger))
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '2.65')
    monkeypatch.delitem(monetary_budget.RATES, 'deepseek-flash')

    with pytest.raises(ValueError, match='deepseek-flash'):
        reserve({'model': 'deepseek-flash', 'max_tokens': 8192})
    with pytest.raises(ValueError, match='未按 0 元记账'):
        reserve({'model': 'deepseek-flash', 'max_tokens': 8192})

    with sqlite3.connect(ledger) as db:
        assert db.execute('SELECT COUNT(*) FROM spend').fetchone()[0] == 0


def test_paid_tools_are_a_different_refusal(tmp_path, monkeypatch):
    """付费工具与"模型没费率"是两件事，各说各的话，不许合并成一句。"""
    monkeypatch.setenv('LAB_COST_LEDGER', str(tmp_path / 'cost.sqlite3'))
    monkeypatch.setenv('LAB_COST_INITIAL_CNY', '2.65')
    with pytest.raises(ValueError, match='付费工具'):
        reserve({'model': 'deepseek-flash', 'max_tokens': 8192, 'tools': [{'type': 'web_search'}]})


@pytest.mark.e2e
def test_cost_scaffold_refuses_to_substitute_another_models_rate():
    """折算脚手架也照同一条规矩：表里缺哪个模型就报错，不挑一个键顶上。"""
    if not COST_SCRIPT.is_file():
        pytest.skip(f'外部成本脚手架不在当前工作区: {COST_SCRIPT}')
    cost = _cost_module()
    rate = cost.load_rate(RATE_SOURCE, ['deepseek-flash'])
    assert rate['models']['deepseek-flash'] == [2, 8]
    assert rate['table']['deepseek-flash'] == (2, 8)
    with pytest.raises(SystemExit, match='deepseek-reasoner'):
        cost.load_rate(RATE_SOURCE, ['deepseek-flash', 'deepseek-reasoner'])
