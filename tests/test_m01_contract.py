import json

import pytest

from backend.domain.errors import ModuleValidationError
from backend.services.requirement_service import RequirementService


def test_m01_response_contract_has_stable_json_fields_and_round_trip():
    service = RequirementService()
    draft = service.extract("培养菌株，每天处理 30 个样品，预算 2 万元")
    payload = draft.to_dict()
    assert set(payload) == {
        "scenario", "task_type", "budget", "throughput", "environment",
        "capabilities", "hard_constraints", "soft_constraints", "unknowns",
        "status", "version",
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    restored = service.confirm(json.loads(encoded))
    assert restored.to_dict()["scenario"] == payload["scenario"]
    assert restored.to_dict()["status"] == "confirmed"


@pytest.mark.parametrize(
    ("value", "code", "field"),
    [
        (None, "invalid_requirement", "requirement"),
        ({"scenario": "采购设备", "hard_constraints": [{"bad": True}]}, "invalid_constraint", "hard_constraints"),
        ({"scenario": "采购设备", "status": "unknown"}, "invalid_status", "status"),
        ({"scenario": "采购设备", "version": "x"}, "invalid_version", "version"),
    ],
)
def test_m01_confirm_rejects_invalid_inputs_with_common_error(value, code, field):
    with pytest.raises(ModuleValidationError) as caught:
        RequirementService().confirm(value)
    error = caught.value
    assert error.code == code
    assert error.field == field
    assert error.to_dict()["code"] == code


def test_m01_confirm_rejects_non_object_edits_with_common_error():
    with pytest.raises(ModuleValidationError) as caught:
        RequirementService().confirm({"scenario": "采购设备"}, edits=[])
    assert caught.value.to_dict() == {
        "code": "invalid_edits", "message": "edits 必须是 JSON 对象", "field": "edits"
    }
