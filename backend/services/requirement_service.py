from __future__ import annotations

import re
from typing import Any

from backend.domain.requirements import Constraint, ConstraintKind, RequirementSpec, RequirementStatus
from backend.domain.requirements_improved import RequirementSpec as ImmutableRequirementSpec


from backend.domain.errors import ModuleValidationError


class RequirementValidationError(ModuleValidationError):
    """输入无法作为采购场景处理时抛出。"""


class RequirementService:
    """将自然语言采购场景转成可编辑的、证据保守的需求草稿。

    这是规则优先的 M01 实现：未出现的条件不会被猜测填充，且每条约束保留原文。
    """

    TASK_TYPES = {
        "培养": "培养/恒温",
        "恒温": "培养/恒温",
        "离心": "离心",
        "PCR": "PCR/扩增",
        "扩增": "PCR/扩增",
        "显微": "显微观察",
        "测序": "测序",
        "分光": "光谱/吸光度分析",
        "吸光": "光谱/吸光度分析",
        "高压灭菌": "灭菌",
        "灭菌": "灭菌",
    }

    def extract(self, scenario: str) -> RequirementSpec:
        if not isinstance(scenario, str) or not scenario.strip():
            raise RequirementValidationError("scenario 必须是非空文本")
        text = " ".join(scenario.strip().split())
        spec = RequirementSpec(scenario=text, task_type=self._task_type(text))
        spec.budget = self._budget(text, spec)
        spec.throughput = self._throughput(text, spec)
        self._environment(text, spec)
        self._capabilities(text, spec)
        self._unknowns(spec)
        # Validate the generated draft through the frozen domain contract
        # before exposing the mutable compatibility DTO to the API layer.
        hardened = ImmutableRequirementSpec.from_dict(spec.to_dict())
        return RequirementSpec.from_dict(hardened.to_dict())

    def confirm(self, spec_or_data: RequirementSpec | dict[str, Any], edits: dict[str, Any] | None = None) -> RequirementSpec:
        if not isinstance(spec_or_data, (RequirementSpec, dict)):
            raise RequirementValidationError("需求草稿必须是 RequirementSpec 或 JSON 对象", code="invalid_requirement", field="requirement")
        if edits is not None and not isinstance(edits, dict):
            raise RequirementValidationError("edits 必须是 JSON 对象", code="invalid_edits", field="edits")
        spec = spec_or_data if isinstance(spec_or_data, RequirementSpec) else RequirementSpec.from_dict(spec_or_data)
        if edits:
            merged = spec.to_dict()
            merged.update(edits)
            spec = RequirementSpec.from_dict(merged)
        if not spec.scenario.strip():
            raise RequirementValidationError("确认需求时 scenario 不能为空")
        # Apply the immutable state transition at the production boundary,
        # then adapt back to the legacy mutable DTO consumed by the API and
        # SQLite repository. Confirmed legacy snapshots are retained for
        # backward compatibility with already-persisted records.
        hardened = ImmutableRequirementSpec.from_dict(spec.to_dict())
        if hardened.status.value == RequirementStatus.CONFIRMED.value:
            return spec
        confirmed = hardened.confirm()
        restored = RequirementSpec.from_dict(confirmed.to_dict())
        # The legacy contract increments version only when edits were
        # supplied; preserve that externally visible behavior.
        restored.version = spec.version + (1 if edits else 0)
        return restored

    def _task_type(self, text: str) -> str | None:
        for word, value in self.TASK_TYPES.items():
            if word in text:
                return value
        return None

    def _budget(self, text: str, spec: RequirementSpec) -> dict[str, Any] | None:
        m = re.search(r"(?:预算(?:上限|不超过|为|约)?|经费|不超过|以内)[：:]?\s*(\d+(?:\.\d+)?)\s*(万元|万|千|元)", text)
        if not m:
            return None
        number, unit = float(m.group(1)), m.group(2)
        multiplier = {"万": 10000, "万元": 10000, "千": 1000, "元": 1}[unit]
        value = int(number * multiplier) if number * multiplier == int(number * multiplier) else number * multiplier
        raw = m.group(0)
        spec.hard_constraints.append(Constraint("budget_max", {"amount": value, "currency": "CNY"}, ConstraintKind.HARD, raw))
        return {"max_amount": value, "currency": "CNY"}

    def _throughput(self, text: str, spec: RequirementSpec) -> dict[str, Any] | None:
        patterns = [
            (r"(?:每天|每日)(?:处理|完成|检测)?\s*(\d+)\s*(?:块板|块微孔板)", "plates_per_day"),
            (r"(?:每天|每日)(?:处理|完成|检测)?\s*(\d+)\s*(?:个|份|批|样品)", "per_day"),
            (r"(\d+)\s*(?:个|份|批|样品)\s*(?:/|每)\s*(?:天|日)", "per_day"),
            (r"一次(?:处理|可处理|最多处理)?\s*(\d+)\s*(?:个|份|批|样品)", "per_run"),
        ]
        for pattern, key in patterns:
            m = re.search(pattern, text)
            if m:
                value = int(m.group(1)); raw = m.group(0)
                result = {"per_day" if key == "plates_per_day" else key: value, "unit": "plates" if key == "plates_per_day" else "samples"}
                spec.hard_constraints.append(Constraint("throughput", result, ConstraintKind.HARD, raw))
                return result
        return None

    def _environment(self, text: str, spec: RequirementSpec) -> None:
        rules = [
            (r"(?:实验室|房间|台面).{0,8}(\d+(?:\.\d+)?)\s*(平方米|平米|m2|㎡)", "space_m2"),
            (r"(\d+(?:\.\d+)?)\s*(?:平方米|平米|m2|㎡)", "space_m2"),
            (r"(220\s*[Vv]|380\s*[Vv])", "power"),
            (r"(\-?\d+(?:\.\d+)?)\s*[至到]\s*(\-?\d+(?:\.\d+)?)\s*[℃°C]", "temperature_range"),
            (r"(无菌|洁净|生物安全柜|通风橱|防爆)", "environment_requirement"),
        ]
        for pattern, name in rules:
            m = re.search(pattern, text, re.I)
            if m:
                value = m.group(0).strip()
                if name == "temperature_range":
                    value = {"min_c": float(m.group(1)), "max_c": float(m.group(2))}
                spec.environment[name] = value
                spec.hard_constraints.append(Constraint(name, value, ConstraintKind.HARD, m.group(0)))

    def _capabilities(self, text: str, spec: RequirementSpec) -> None:
        # Preserve explicit user conditions verbatim; classify each clause locally.
        clauses = re.split(r"[，。；;\n]", text)
        explicit = set()
        for clause in clauses:
            match = re.search(r"(必须(?:支持|具备|满足)?|优先考虑|优先|最好)(.+)", clause)
            if not match:
                continue
            explicit.add(clause)
            kind = ConstraintKind.HARD if match.group(1).startswith("必须") else ConstraintKind.SOFT
            for value in re.split(r"[、]", match.group(2)):
                value = value.strip()
                if value:
                    spec.capabilities.append(value)
                    target = spec.hard_constraints if kind is ConstraintKind.HARD else spec.soft_constraints
                    target.append(Constraint("capability", value, kind, match.group(0)))
        capability_map = {
            "自动记录": "自动记录/导出数据", "数据导出": "自动记录/导出数据", "联网": "联网或数据接口",
            "温度均匀": "温度均匀性可验证", "制冷": "制冷", "程序控温": "程序控温",
            "低温": "低温运行", "紫外": "紫外功能", "转速": "转速可调",
        }
        for clause in clauses:
            if clause in explicit:
                for word, capability in capability_map.items():
                    if word in clause and capability not in spec.capabilities:
                        spec.capabilities.append(capability)
                continue
            for word, capability in capability_map.items():
                if word in clause and capability not in spec.capabilities:
                    spec.capabilities.append(capability)
                    kind = ConstraintKind.HARD if any(x in clause for x in ("必须", "需要", "要求", "要有")) else ConstraintKind.SOFT
                    spec.hard_constraints.append(Constraint("capability", capability, kind, word)) if kind is ConstraintKind.HARD else spec.soft_constraints.append(Constraint("capability", capability, kind, word))

    def _unknowns(self, spec: RequirementSpec) -> None:
        if spec.task_type is None: spec.unknowns.append("任务类型")
        if spec.budget is None: spec.unknowns.append("预算上限（含税、安装和附件）")
        if spec.throughput is None: spec.unknowns.append("样品量或处理通量")
        if not spec.environment: spec.unknowns.append("场地、电源、温湿度和安全环境限制")
        if not spec.capabilities: spec.unknowns.append("必须具备的功能")
