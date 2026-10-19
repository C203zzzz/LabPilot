# -*- coding: utf-8 -*-
"""费率表覆盖：缺项必须响亮失败，不许静默算 0、也不许拿别的模型的费率顶上。

默认模型 ``deepseek-flash`` 必须能在 ``monetary_budget.RATES`` 里查到费率：查不到时
只要一设 ``LAB_COST_LEDGER``，整条通道就直接拒发；任何"挑一个带 deepseek 的键顶上"
的兜底都会把未知费率冒充成已知金额。

费率来源（不许凭记忆）：官方价目表
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ 写明模型名 ``deepseek-flash``
的模型版本就是 DeepSeek-V4.1-Flash，输入（缓存未命中）高峰 2 元/百万、输出高峰
8 元/百万，空闲时段减半（1 / 4）。
"""
import sqlite3

import pytest

from backend.infrastructure import monetary_budget
from backend.infrastructure.deepseek import DeepSeekClient
from backend.infrastructure.monetary_budget import RATES, rate_for, reserve

#: 官方价目表上的高峰时段价（元/百万 token，输入缓存未命中 / 输出）。
OFFICIAL_FLASH_PEAK = (2, 8)


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
