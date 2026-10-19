"""Optional deployment spend ceiling, durable across process restarts.

Reserve worst-case byte/token bounds before sending. Failed/unknown requests
retain reservations. No keys, prompts or model content enter this ledger.

金额只覆盖**模型调用**：第三方检索通道（CCGP／Exa／网页抓取等）在
``usage_ledger`` 里只按**次数**记账，没有费率、**不计入这里的金额**——要报钱
时只能写"未计费"，不许当成 0 元。
"""
import json
import os
import sqlite3
import uuid
from pathlib import Path

#: 元 / 百万 token，``(输入缓存未命中, 输出)``，**按高峰时段价预留**（最坏情况）；
#: 空闲时段是高峰价的一半（同一个模型：``deepseek-flash`` 1/4、``qwen3.8-max-0902`` 4.5/13.5），
#: 对账时按实际时段另算。
#: 来源：DeepSeek 官方价目表 https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
#: （2026-10-01 取）：模型名 ``deepseek-flash`` 的模型版本就是 DeepSeek-V4.1-Flash，
#: 输入（缓存未命中）高峰 2 元、输出高峰 8 元；``deepseek-v4.1-flash`` 是同版本在网关上
#: 的写法，故同价。本地交叉核对：``docs/78:355`` 与 ``output/e2e-final/runs/*/cost.json``
#: 里真实外发的模型串是 ``deepseek-flash``，金额正是按 (2, 8) 折出来的。
#: **表里没有的模型一律拒发**（``reserve`` 抛错）：不许拿别的模型的费率顶上，也不许把
#: 未知金额当 0（口径见 ``docs/209`` §3.4）。
RATES = {
    'deepseek-flash': (2, 8),
    'deepseek-v4.1-flash': (2, 8),
    'qwen3.8-max-0902': (12, 36),
}


class BudgetConfigError(ValueError):
    """金额闸门的**配置错误**：响亮失败，绝不替部署方编一个默认额度。

    与 ``reserve`` 抛的其它 ``ValueError``（额度到顶、模型没费率、付费工具未覆盖）
    不同，这类错误出在**配置层**：闸门打开了，却没告诉账本"已经花了多少"。
    所以它自带 ``code``，由 ``deepseek.py`` 的 ``_call`` 原样转述——不许粗化成
    "额度超了"，那是另一件事实（口径：``AGENTS.md``「对外口径与兜底」，``docs/209`` §3）。
    继承 ``ValueError`` 是为了让既有的调用侧照旧接得住。
    """

    code = 'monetary_budget_not_configured'

    def __init__(self, message: str, variable: str | None = None):
        super().__init__(message)
        #: 出问题的环境变量名，给排查用。
        self.variable = variable


#: ``_amount_from_env`` 的哨兵：区分"这个变量必须显式给出"与"允许缺省"。
_UNSET = object()


def _amount_from_env(name: str, default):
    """读一个金额环境变量，读不到就抛点名变量的人话错误。

    ``default`` 是 ``_UNSET`` 表示这个变量**必须显式给出**（``LAB_COST_INITIAL_CNY``）；
    否则表示允许缺省（``LAB_COST_LIMIT_CNY`` 缺省 2.8）。**但设了却给不出数字
    （含设成空值）一律算配置错误，不拿默认值顶上去**——按 0 元或按默认上限起算
    都会把"装错了"变成"静默少做一步"。
    """
    raw = os.environ.get(name)
    if raw is None and default is not _UNSET:
        return default
    if raw is not None and raw.strip():
        try:
            return float(raw.strip())
        except ValueError:
            pass
    shown = '(未设)' if raw is None else repr(raw)
    if default is _UNSET:
        raise BudgetConfigError(
            '金额闸门已打开（已设 LAB_COST_LEDGER），但 %s %s——这两个变量必须配套：'
            'LAB_COST_LEDGER 指定账本文件，%s 给出账本启用时已经花掉的钱数（元）。'
            '这里不替它编默认值：按 0 元起算会把真实花费算漏，所以未发送任何请求、'
            '也未写入账本。' % (name, shown, name), variable=name)
    raise BudgetConfigError(
        '%s 必须是数字（单位元），当前是 %s；这里不拿默认值顶上去，也未发送请求。'
        % (name, shown), variable=name)


def rate_for(model):
    """这个模型的 ``(输入, 输出)`` 费率；费率表没有就返回 ``None``。

    返回 ``None`` 的语义只有一个：**费率未知、这笔钱算不出来**。调用方必须如实标注
    "未计费／费率未知"，不许当成 0 元。
    """
    return RATES.get(str(model or '').strip().lower())


def connection():
    path = os.getenv('LAB_COST_LEDGER')
    if not path:
        return None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS spend (id TEXT PRIMARY KEY, model TEXT, reserved REAL, charged REAL, state TEXT)')
    db.execute('BEGIN IMMEDIATE')
    return db


def reserve(payload):
    db = connection()
    if db is None:
        return None
    try:
        model = payload.get('model', '').lower()
        if payload.get('tools'):
            raise ValueError('当前费用计划未覆盖付费工具，未发送请求。')
        if rate_for(model) is None:
            raise ValueError('费率表没有模型 %s 的费率，这笔钱折算不出来；'
                             '未按 0 元记账，也未发送请求。' % (payload.get('model') or '(空模型名)'))
        seed = _amount_from_env('LAB_COST_INITIAL_CNY', _UNSET)
        ceiling = min(2.8, _amount_from_env('LAB_COST_LIMIT_CNY', 2.8))
        input_rate, output_rate = rate_for(model)
        bound = len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + 1024
        reserve_cny = (bound * input_rate + int(payload.get('max_tokens', 8192)) * 2 * output_rate) / 1e6
        spent = db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM spend').fetchone()[0]
        if seed + spent + reserve_cny > ceiling:
            raise ValueError('本轮真实测试费用预留已到上限，已有结果已保留；可继续本地核验。')
        ident = uuid.uuid4().hex
        db.execute('INSERT INTO spend VALUES (?,?,?,?,?)', (ident, model, reserve_cny, None, 'reserved'))
        db.commit()
        return ident, input_rate, output_rate
    finally:
        db.close()


def settle(ticket, usage):
    if ticket is None or not isinstance(usage, dict) or 'prompt_tokens' not in usage or 'completion_tokens' not in usage:
        return
    ident, input_rate, output_rate = ticket
    reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
    charged = (usage['prompt_tokens'] * input_rate + (usage['completion_tokens'] + reasoning) * output_rate) / 1e6
    db = connection()
    try:
        db.execute('UPDATE spend SET charged=?, state=? WHERE id=?', (charged, 'completed', ident))
        db.commit()
    finally:
        db.close()
