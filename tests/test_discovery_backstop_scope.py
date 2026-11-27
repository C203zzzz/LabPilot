# -*- coding: utf-8 -*-
"""规则兜底要扫**全部**发现来源，不是模型窗口那 8 篇。

取证（``output/sample-b-stability/report.md`` §三、§五.1）：``alternatives()`` 里兜底
过去是 ``text_scan_candidates(sources[:DISCOVERY_WINDOW], base)``，而每轮进窗口的 8 篇
几乎不重叠（brand1 那轮 41 条发现来源只给 8 条），于是同一套兜底规则一轮捞 10 条、
一轮只捞 1 条。窗口本身**不删**：它管的是送进模型的那部分正文（模型成本）；
兜底是纯本地正文扫描，零网络、零模型成本，没有理由被同一个窗口卡住。
"""
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

BASELINE = {'brand': '甲厂', 'model': 'Q10', 'category': '超声波清洗机', 'scenario': '采购一台超声波清洗机',
            'parameters': [{'name': '容量', 'value': 100, 'unit': 'L', 'direction': 'min'}]}
TARGET_URL = 'https://scientz.example/style/product/12994.html'
TARGET_LINE = '槽式超声波清洗机 乙厂 Q20 技术参数 容量 100 L'


def _page(url, text, purpose='discovery'):
    return {'url': url, 'purpose': purpose, 'body_status': 'fetched',
            'source_type': 'manufacturer_website', 'body_text': text}


def _candidate_page_sits_outside_the_model_window():
    """12 篇发现来源，候选那一页被模型窗口挤在外面。

    ``discovery_sources`` 先 ``reversed(sources)`` 再稳定按 purpose 排序，所以窗口那
    8 篇取的是列表**尾部**；候选页放在列表头部 => 它落到第 12 位 => 窗口外。
    """
    fillers = [_page(f'https://filler-{index}.example/page', '某实验室设备采购公告 正文 数量1台')
               for index in range(11)]
    return [_page(TARGET_URL, TARGET_LINE)] + fillers


def test_the_rule_backstop_finds_a_candidate_outside_the_eight_page_window(research, monkeypatch):  # noqa: F811
    service, run, _, _ = research
    run.update({'baseline': BASELINE, 'sources': _candidate_page_sits_outside_the_model_window(),
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': []})
    window_sizes = []

    def fake_model(run_, prompt, data):
        if '其他仪器' in prompt:
            window_sizes.append(len(data['sources']))
        return {'candidates': []}          # 模型这一轮没提出候选 => 走规则兜底

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)

    service.alternatives(run)

    models = [candidate['model'] for candidate in run['discovered_candidates']]
    assert 'Q20' in models, models
    # 兜底命中的那一页正是窗口外那一页（它是这条候选唯一的出处）。
    assert next(c for c in run['discovered_candidates'] if c['model'] == 'Q20')['url'] == TARGET_URL
    # 窗口机制本身一个字没动：送进模型的仍然只有 8 篇。
    assert window_sizes == [8], window_sizes
