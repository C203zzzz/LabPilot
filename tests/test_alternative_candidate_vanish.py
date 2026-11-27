# -*- coding: utf-8 -*-
"""候选不许凭空消失：进了核验名额的候选，最后必须在正式候选或参考候选之一。

真跑实测（``设计规范`` 第十五节，落盘证据 ``output/alt-extract/live-extract7.json``）：
北京莱普特 ``C1450`` 被 ``diverse_candidates`` 选进了深入核验名额，却在核验入口被判
"来源不可用"（``price_lookup = {'status': 'not_attempted', 'reason_code':
'source_not_usable'}``），于是**既不在 ``alternatives``、也不在 ``reference_candidates``**
——用户眼里这条候选消失了。同一张发现页上的 ``H1650`` 却走完了核验（``status: refused``），
唯一差别是 C1450 的原文行把型号写成 ``C1450-230V``（型号 + 连字符后缀）。

根因：核验入口那一句"这条来源讲的是不是这个候选型号"用的是 ``contains_model``，而
``设计规范`` 把它的边界收紧成"左右紧邻的连字符不再算分隔符"（``SPX-250B-ZII`` 里的
``SPX-250B`` 不许命中），于是 ``contains_model('C1450-230V高速离心机 微型高速离心机',
'C1450')`` 是 ``False``。

这一组用例钉两件事：

1. **入口改用局部识别**（``candidate_source_matches_model``）：认"型号 + 连字符后缀"，
   但 ``contains_model`` 本身与 ``设计规范`` 的收紧口径一个字不动；
2. **不变量**：凡进了核验名额的候选，最后必须在 ``alternatives`` 或
   ``reference_candidates`` 里（``place_slotted_candidates`` 兜底）。

真跑那两份正文（发现页 + C1450/H1650 详情页）落在
``output/report-llm/_probe/body_yiqi_12588707.txt`` 与 ``output/alt-extract/live-probe/``；
它们是本仓的工作产物、不在版本库里，所以依赖它们的用例在文件缺失时跳过——一旦在，
判据就是拿真数据跑的。
"""
import json
import os

import pytest

from backend.services.procurement_research import ON_DEMAND_ENV
from backend.services.procurement_rules import contains_model
from backend.services.research_planning import (candidate_source_matches_model, diverse_candidates)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, 'output', 'report-llm', '_probe', 'body_yiqi_12588707.txt')
LIVE = os.path.join(ROOT, 'output', 'alt-extract', 'live-probe')
#: 真跑那一轮发现页上 C1450 与 H1650 的原文行/候选行（``live-extract7.json`` 逐字）。
C1450_LINE = 'C1450-230V高速离心机 微型高速离心机'
H1650_LINE = '高速离心机-湘仪H1650台式高速离心机'

BASELINE = {
    'brand': '湖南湘仪', 'model': 'H1850R', 'category': '离心机',
    'scenario': '采购一台高速离心机，用于实验室日常检测',
    'parameters': [{'name': '最高转速', 'value': 18000, 'unit': 'r/min', 'direction': 'min'}],
}
DISCOVERY_URL = 'https://www.yiqi.com/product/detail_12588707.html'


def _probe_body(name):
    path = os.path.join(LIVE, name)
    if not os.path.exists(path):
        pytest.skip('真跑正文不在工作区：%s' % path)
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _discovery_body():
    if not os.path.exists(PROBE):
        pytest.skip('真跑发现页正文不在工作区：%s' % PROBE)
    with open(PROBE, encoding='utf-8') as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# 一、判据：入口要认出"型号 + 连字符后缀"，但全局判据不许动
# ---------------------------------------------------------------------------

def test_the_global_model_predicate_still_refuses_a_hyphen_suffix():
    """``contains_model`` 是收紧过的全局判据：这一条**不许**因为本次修复变宽。"""
    assert contains_model(C1450_LINE, 'C1450') is False
    assert contains_model('SPX-150B', 'SPX-150BIII') is False
    assert contains_model('SPX-250B-ZII', 'SPX-250B') is False
    assert contains_model('TBSPX-150BIII', 'SPX-150BIII') is False


def test_the_entry_predicate_reads_a_hyphen_suffix_as_the_same_instrument():
    """入口那一层：``C1450-230V`` 这一页讲的**就是**候选 C1450。"""
    assert candidate_source_matches_model(C1450_LINE, 'C1450') is True
    assert candidate_source_matches_model('C10K-230V 手掌高速离心机', 'C10K') is True
    assert candidate_source_matches_model('FC5513-R01微量高速离心机', 'FC5513') is True
    # 厂商印的全角连字符与"型号 + 空格 + 后缀"两种写法同样认。
    assert candidate_source_matches_model('C1450－230V高速离心机', 'C1450') is True
    assert candidate_source_matches_model('FC5513 R01微量高速离心机', 'FC5513') is True
    # 全局判据认得的那一种照旧认得。
    assert candidate_source_matches_model(H1650_LINE, 'H1650') is True
    assert candidate_source_matches_model('Cary 60 分光光度计', 'Cary 60') is True


def test_the_entry_predicate_does_not_turn_another_model_into_the_candidate():
    """放宽只到"同一个型号的另一种写法"为止：别的型号一个都不许混进来。

    后缀里**必须有数字**才算同一种写法。``R01``／``230V`` 是配置或电源写法，认；
    ``SPX-250B-ZII`` 的 ``ZII``、``SPX-150BIII`` 的 ``III`` 是同一族里另外的型号
    （纯字母后缀），``1300A2`` 对 ``1300A2III`` 也一样——``设计规范`` 的护栏就是这么立的。
    """
    assert candidate_source_matches_model(H1650_LINE, 'H1750') is False
    assert candidate_source_matches_model('FC5714高速离心机', 'FC5816') is False
    assert candidate_source_matches_model('SPX-250B-ZII', 'SPX-250B') is False
    assert candidate_source_matches_model('SPX-150BIII | 500W', 'SPX-150B') is False
    assert candidate_source_matches_model('1300A2III 生物安全柜', '1300A2') is False
    assert candidate_source_matches_model('TBSPX-150BIII', 'SPX-150BIII') is False
    # 反向：这一页写的是 FC5816-R01，候选是 FC5816 时**不算**同一台（后缀对不上型号）。
    assert candidate_source_matches_model('FC5816高速离心机', 'FC5816-R01') is False
    # ``C14500`` 不是 ``C1450``：型号那一串后面还有数字。
    assert candidate_source_matches_model('C14500高速离心机', 'C1450') is False


# ---------------------------------------------------------------------------
# 二、真跑数据：C1450 必须进得了核验名额
# ---------------------------------------------------------------------------

def test_the_real_discovery_page_lets_c1450_through_the_entry_gate():
    """用真跑那一轮的发现页正文：改前 ``contains_model`` 判否、候选进不了名额。"""
    body = _discovery_body()
    assert C1450_LINE in body.replace('\n', '') or C1450_LINE in body
    from backend.services.procurement_rules import supported
    assert supported(C1450_LINE, body) is True
    assert contains_model(C1450_LINE, 'C1450') is False          # 改前的拦路判据
    assert candidate_source_matches_model(C1450_LINE, 'C1450') is True
    # 对照：同一页的 H1650 改前就过得去——两者唯一的差别就是那个连字符后缀。
    assert contains_model(H1650_LINE, 'H1650') is True


# ---------------------------------------------------------------------------
# 三、不变量：进了名额就必须有落点
# ---------------------------------------------------------------------------

def _invariant(run):
    """凡进了核验名额的候选，都要能在正式候选或参考候选里找到。"""
    landed = {(str(c.get('brand') or '').strip(), str(c['model']).strip())
              for c in list(run['alternatives']) + list(run['reference_candidates'])}
    return [c['model'] for c in run['discovered_candidates']
            if (str(c.get('brand') or '').strip(), str(c['model']).strip()) not in landed]


def test_a_candidate_that_fails_inside_verification_still_lands_in_the_reference_list(research, monkeypatch):  # noqa: F811
    """核验过程里单点失败（``candidate_scope`` 就地跳过）不许把候选吞掉。

    改前：这一步失败只写进缺口，候选既不在 ``alternatives``（没跑完）也不在
    ``reference_candidates``（``diverse_candidates`` 选它时它还是"正式候选"），于是
    两头都没有。改后：``place_slotted_candidates`` 把它补进参考候选。

    核验是在**批量查价**那条路上跑的（开关打开时 ``alternatives()`` 不核验、不会炸），
    所以这里显式把开关关掉。
    """
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    body = ('离心机采购项目\n'
            '乙厂 Q20 台式高速离心机\n'
            '某医院实验室设备采购项目中标（成交）结果公告\n')
    source = {'url': 'https://hospital-a.example.com/notice/1', 'purpose': 'discovery',
              'body_status': 'fetched', 'source_type': 'unknown',
              'title': '离心机采购项目中标结果公告', 'body_text': body}
    run.update({'baseline': {**BASELINE, 'version': run['baseline'].get('version')},
                'sources': [source], 'alternatives': [], 'discovered_candidates': [],
                'reference_candidates': [], 'prices': [], 'gaps': [], 'price_rejections': [],
                'price_source_skips': []})

    def fake_model(run_, prompt, data):
        if '从给定正文发现' in prompt:
            # ``configuration`` 不能空：2026-10-06 起"找不到配置的候选不展示"
            # （``tests/test_m4_pipeline.py``），空配置的候选会在收尾时被撤下候选卡片。
            # 这一条测的是**核验过程里单点失败仍然有落点**，所以给一段正文里逐字存在的配置。
            return {'candidates': [{'brand': '乙厂', 'model': 'Q20', 'url': source['url'],
                                    'configuration': '乙厂 Q20 台式高速离心机',
                                    'excerpt': '乙厂 Q20 台式高速离心机'}]}
        return {'parameters': [], 'queries': []}

    def exploding_prices(*args, **kwargs):
        raise RuntimeError('抽价这一步在这一条候选上炸了')

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', exploding_prices)
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)

    assert [c['model'] for c in run['discovered_candidates']] == ['Q20']
    # 失败半径还是这一个候选：整条车道没塌，缺口里留了痕。
    assert any('Q20' in gap for gap in run['gaps']), run['gaps']
    # 但它**不许消失**：核验没跑完，就按参考候选列出来，并说明为什么。
    assert [c['model'] for c in run['alternatives']] == []
    landed = [c for c in run['reference_candidates'] if c['model'] == 'Q20']
    assert landed, run['reference_candidates']
    assert landed[0]['reference_reason'] == 'no_conclusion'
    assert _invariant(run) == []


def test_a_candidate_refused_at_the_entry_gate_does_not_vanish(research, monkeypatch):  # noqa: F811
    """入口被判不可用的候选，也要有落点（这条钉的正是 C1450 那种形状）。"""
    service, run, _, _ = research
    body = ('离心机采购项目\n'
            '乙厂 Q20-230V 台式高速离心机\n'
            '某医院实验室设备采购项目中标（成交）结果公告\n')
    source = {'url': 'https://hospital-a.example.com/notice/2', 'purpose': 'discovery',
              'body_status': 'fetched', 'source_type': 'unknown',
              'title': '离心机采购项目中标结果公告', 'body_text': body}
    run.update({'baseline': {**BASELINE, 'version': run['baseline'].get('version')},
                'sources': [source], 'alternatives': [], 'discovered_candidates': [],
                'reference_candidates': [], 'prices': [], 'gaps': [], 'price_rejections': [],
                'price_source_skips': []})

    def fake_model(run_, prompt, data):
        if '从给定正文发现' in prompt:
            # ``excerpt`` 里型号写成"型号 + 连字符后缀"，真跑那一轮 C1450 就是这么写的。
            # ``configuration`` 同上一条：空配置的候选会被新的展示口径撤下。
            return {'candidates': [{'brand': '乙厂', 'model': 'Q20', 'url': source['url'],
                                    'configuration': '乙厂 Q20-230V 台式高速离心机',
                                    'excerpt': '乙厂 Q20-230V 台式高速离心机'}]}
        return {'parameters': [], 'queries': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: [])
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)

    # 入口改造之后它**进得了名额**：所以它会走完核验、落进正式候选，而不是被记成
    # 「来源不可用」丢掉。这一条同时是"不再凭空消失"的正向断言。
    assert [c['model'] for c in run['discovered_candidates']] == ['Q20']
    assert _invariant(run) == []
    assert [c['model'] for c in run['alternatives']] == ['Q20']


def test_the_safety_net_leaves_an_already_placed_candidate_alone():
    """已经在正式候选或参考候选里的，兜底一个都不重复补。"""
    from backend.services.procurement_research import ProcurementResearch
    service = object.__new__(ProcurementResearch)
    candidate = {'brand': '乙厂', 'model': 'Q20', 'url': 'u', 'excerpt': 'e'}
    run = {'alternatives': [{'brand': '乙厂', 'model': 'Q20'}], 'reference_candidates': []}
    service.place_slotted_candidates(run, {('乙厂', 'Q20')}, [candidate])
    assert run['reference_candidates'] == []
    run = {'alternatives': [], 'reference_candidates': [{'brand': '乙厂', 'model': 'Q20',
                                                         'reference_reason': 'same_brand'}]}
    service.place_slotted_candidates(run, {('乙厂', 'Q20')}, [candidate])
    assert [c['reference_reason'] for c in run['reference_candidates']] == ['same_brand']


def test_the_safety_net_never_touches_a_candidate_that_never_got_a_slot():
    """没进名额的候选（``diverse_candidates`` 直接判成参考候选的）由 ``diverse_candidates``
    自己落点，兜底不许再补一遍——否则同一条候选会出现两张卡片。"""
    from backend.services.procurement_research import ProcurementResearch
    service = object.__new__(ProcurementResearch)
    candidate = {'brand': '乙厂', 'model': 'Q20+', 'url': 'u', 'excerpt': 'e'}
    run = {'alternatives': [], 'reference_candidates': []}
    service.place_slotted_candidates(run, set(), [candidate])
    assert run['reference_candidates'] == []


# ---------------------------------------------------------------------------
# 四、真跑那一轮的落点对照（有落盘证据时跑）
# ---------------------------------------------------------------------------

def test_the_real_run_keeps_c1450_as_a_reference_when_config_fails():
    """落盘证据里的 C1450 配置明确低于要求，必须保留但不得查价或晋级。"""
    path = os.path.join(ROOT, 'output', 'alt-extract', 'live-extract7.json')
    if not os.path.exists(path):
        pytest.skip('真跑落盘结果不在工作区：%s' % path)
    with open(path, encoding='utf-8') as handle:
        saved = json.load(handle)

    models = [c['model'] for c in saved['discovered_candidates']]
    assert 'C1450' in models and 'H1650' in models
    c1450 = next(c for c in saved['discovered_candidates'] if c['model'] == 'C1450')
    assert c1450['excerpt'] == C1450_LINE
    assert c1450['price_lookup']['status'] == 'not_attempted'
    assert c1450['price_lookup']['reason_code'] == 'source_not_usable'
    # 现场：改前那一轮它两头都没有（不在正式候选、也不在参考候选）。
    assert 'C1450' not in [c['model'] for c in saved['alternatives']]
    assert 'C1450' not in [c['model'] for c in saved['reference_candidates']]
    # 新口径：逐项配置证据明确不满足时，候选进入参考名单并说明原因，不能进入正式名额。
    selected, references = diverse_candidates(saved['discovered_candidates'], BASELINE['brand'])
    assert 'C1450' not in [c['model'] for c in selected]
    c1450_reference = next(c for c in references if c['model'] == 'C1450')
    assert c1450_reference['reference_reason'] == 'config_hard_constraint'
