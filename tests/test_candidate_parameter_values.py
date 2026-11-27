# -*- coding: utf-8 -*-
"""Specification-table values must never be promoted to product models.

A brochure's parameter table is a grid of labels and values.
``| 电源 | AC 220±22V 50/60Hz |`` is a voltage; a real run stored "AC 220" as a
candidate and left the model field holding a power supply. The rules under test
are generic (unit symbols, standard numbers, parameter-row labels) and were
checked against the real brochures in the project fixtures, so the same tests
also pin that genuine models in those tables survive.

* value rows -> dropped;
* models such as XKA-2200 / SPX-150BIII / Infinite 200 PRO -> kept;
* the model-output path (DeepSeek inventory) applies the same rule, because a
  prompt alone does not guarantee it.
"""
import pytest

from backend.services.candidate_service import (
    ParsedPage,
    extract_candidates,
    measurement_value_not_model,
)
from backend.services.deepseek_analysis import CANDIDATE_INVENTORY_PROMPT, DeepSeekAnalysisService

# Values taken from real brochures: voltage/power/frequency rows, an interface
# row and a standard number. None of them is a product.
SPECIFICATION_VALUES = [
    ('AC 220', '电源 AC 220±22V 50/60Hz'),
    ('AC220V', '输入功率 500W 800W 电源 AC220V S0HZ 工作环境温度 5-35℃'),
    ('AC220', '| 电源 | AC220V 50HZ |'),
    ('ISO8655-', '量程 增量 测量体积 ISO8655-2规定的最大误差'),
    ('RS-232C', '计算机接口 USB 接口或 RS-232C 串口'),
    ('DC12', '工作电压 DC12V'),
]
# Models those same documents really carry.
REAL_MODELS = [
    ('XKA-2200', '| 型号 | XKA-2200 |'),
    ('SPX-150BIII', '主要技术参数： 型号 SPX-70BIII SPX-70BX SPX-150BIII SPX-150BX'),
    ('LRH-1000', '技术参数 型号 LRH-080(H) LRH-150(H) LRH-250(H) LRH-1000(H) 控温范围 0-70℃'),
    ('YK-S160', '合肥右科仪器设备有限公司 电话：0551-67880302 YK-S160 智能生化培养箱 技 术 参 数'),
    ('Infinite 200 PRO', '基于广受欢迎的 Infinite 200 PRO 检测平台和模块化设计'),
    ('KF 96', '适用板型 KF 24 深孔板、KF 96 深孔板、KF 96 浅孔板、96 PCR 板'),
    ('Q10', 'Model Q10'),
    ('APEX-2000', 'Model APEX-2000, configuration: 96-well deck'),
    ('A100', '型号 A100 恒温培养箱'),
]


@pytest.mark.parametrize('model,evidence', SPECIFICATION_VALUES)
def test_specification_values_are_not_models(model, evidence):
    assert measurement_value_not_model(model, evidence)


@pytest.mark.parametrize('model,evidence', REAL_MODELS)
def test_real_models_are_kept(model, evidence):
    assert not measurement_value_not_model(model, evidence)


def test_a_parameter_row_never_becomes_a_candidate():
    page = ParsedPage('d', 'page:1', '| 电源 | AC 220±22V 50/60Hz |\n| 型号 | XKA-2200 |')
    assert [candidate.model for candidate in extract_candidates([page])] == ['XKA-2200']


def test_a_real_specification_table_keeps_its_models_and_drops_its_values():
    page = ParsedPage('d', 'page:1',
                      '主要技术参数 型号 XKA-2200 方式 强制对流 使用温度范围 0~65℃ '
                      '输入功率 800W 电源 AC220V S0HZ 工作室尺寸(mm) 400*500*600')
    assert [candidate.model for candidate in extract_candidates([page])] == ['XKA-2200']


def test_a_value_only_page_yields_no_candidate():
    page = ParsedPage('d', 'page:1', '| 输入功率 | 800 W |\n| 电源 | AC 220V 50Hz |')
    assert extract_candidates([page]) == []


def _inventory_service(models):
    class Model:
        def analyze(self, system_prompt, user_prompt):
            return {'candidates': [{'model': model, 'sources': [{'document_id': 'd',
                                                                 'page_or_location': 'page:1',
                                                                 'excerpt': model}]}
                                   for model in models]}
    return DeepSeekAnalysisService(Model())


def test_the_model_inventory_path_applies_the_same_rule():
    documents = [{'id': 'd', 'pages': [{'number': 1, 'source_location': 'page:1',
                                        'text': '型号 XKA-2200 电源 AC 220±22V 50/60Hz'}]}]
    service = _inventory_service(['AC 220', 'XKA-2200'])
    result = service.analyze(scenario='微生物培养', documents=documents)
    assert [candidate['model'] for candidate in result['candidates']] == ['XKA-2200']
    assert result['candidates'][0]['status'] == 'pending_review'
