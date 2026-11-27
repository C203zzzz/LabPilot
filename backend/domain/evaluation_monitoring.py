"""M13 evaluation and quality-monitoring contracts.

The module is intentionally offline and deterministic.  It evaluates a
caller-supplied run against a small, versioned fixture set and never mutates
upstream procurement objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricValue:
    name: str
    value: float
    denominator: int
    numerator: int
    unit: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "numerator": self.numerator,
                "denominator": self.denominator, "unit": self.unit, "note": self.note}


@dataclass(frozen=True)
class FailureRecord:
    case_id: str
    stage: str
    anomaly_class: str
    reproducible: bool
    expected: str
    observed: str
    reproduction: str
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EvaluationReport:
    dataset_version: str
    run_id: str
    metrics: tuple[MetricValue, ...]
    failures: tuple[FailureRecord, ...] = ()
    anomalies: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"dataset_version": self.dataset_version, "run_id": self.run_id,
                "metrics": [m.to_dict() for m in self.metrics],
                "failures": [f.to_dict() for f in self.failures],
                "anomalies": dict(sorted(self.anomalies.items()))}
