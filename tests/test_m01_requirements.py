from backend.domain.requirements import RequirementStatus
from backend.services.requirement_service import RequirementService, RequirementValidationError


def test_extracts_hard_soft_and_unknowns_without_guessing():
    spec = RequirementService().extract("兽医微生物实验室做菌株培养，每天处理 40 个样品，预算不超过 8 万元，必须自动记录温度，台面空间 2 平方米")
    assert spec.task_type == "培养/恒温"
    assert spec.throughput == {"per_day": 40, "unit": "samples"}
    assert spec.budget["max_amount"] == 80000
    assert "自动记录/导出数据" in spec.capabilities
    assert any(c.kind.value == "hard" for c in spec.hard_constraints)
    assert "任务类型" not in spec.unknowns
    assert "场地、电源、温湿度和安全环境限制" not in spec.unknowns


def test_unknown_information_is_preserved():
    spec = RequirementService().extract("想买一台实验室设备，用于日常实验")
    assert spec.task_type is None
    assert spec.budget is None
    assert spec.unknowns
    assert spec.status is RequirementStatus.DRAFT


def test_confirm_is_editable_and_versioned():
    service = RequirementService()
    draft = service.extract("做 PCR，每天 10 个样品")
    confirmed = service.confirm(draft, {"budget": {"max_amount": 50000, "currency": "CNY"}, "unknowns": []})
    assert confirmed.status is RequirementStatus.CONFIRMED
    assert confirmed.version == 2
    assert confirmed.budget["max_amount"] == 50000


def test_empty_input_is_rejected():
    try:
        RequirementService().extract("  ")
    except RequirementValidationError:
        return
    raise AssertionError("empty scenario should fail")


def test_real_plate_reader_scenario_preserves_units_and_preferences():
    spec = RequirementService().extract('采购酶标仪，必须支持96孔板、450nm吸光检测，预算上限10万元；优先考虑温控、振荡、数据导出。每天检测2块板。')
    assert spec.budget['max_amount'] == 100000
    assert spec.throughput == {'per_day': 2, 'unit': 'plates'}
    assert {'96孔板', '450nm吸光检测'} <= {c.value for c in spec.hard_constraints if c.name == 'capability'}
    assert {'温控', '振荡', '数据导出'} <= {c.value for c in spec.soft_constraints}
    assert not any(c.value == '自动记录/导出数据' for c in spec.hard_constraints)
