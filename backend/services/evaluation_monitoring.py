"""Deterministic M13 quality metrics for extracted procurement runs."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from backend.domain.evaluation_monitoring import EvaluationReport, FailureRecord, MetricValue


def _metric(name: str, numerator: int, denominator: int, unit: str = "ratio", note: str = "") -> MetricValue:
    return MetricValue(name, round(numerator / denominator if denominator else 0.0, 4), denominator, numerator, unit, note)


def evaluate_cases(cases: Iterable[dict[str, Any]], *, run_id: str = "local", dataset_version: str = "m13-v1") -> EvaluationReport:
    """Score fixture case records; records are plain JSON and deterministic.

    Expected fields are ``expected_candidates``, ``observed_candidates``,
    ``expected_citations``, ``observed_citations``, ``expected_recommendation``
    and ``observed_recommendation``.  A case may include ``failure`` to retain
    a reproducible anomaly rather than hiding it from aggregate metrics.
    """
    rows = list(cases)
    extraction_num = citation_num = recommendation_num = 0
    candidate_tp = candidate_fp = candidate_fn = 0
    failures: list[FailureRecord] = []
    anomaly_counts: Counter[str] = Counter()
    for row in rows:
        expected = set(row.get("expected_candidates", []))
        observed = set(row.get("observed_candidates", []))
        if expected == observed:
            extraction_num += 1
        candidate_tp += len(expected & observed)
        candidate_fp += len(observed - expected)
        candidate_fn += len(expected - observed)
        exp_c = set(row.get("expected_citations", []))
        obs_c = set(row.get("observed_citations", []))
        if exp_c <= obs_c:
            citation_num += 1
        if row.get("expected_recommendation") == row.get("observed_recommendation"):
            recommendation_num += 1
        failure = row.get("failure")
        if failure:
            record = FailureRecord(case_id=row["case_id"], **failure)
            failures.append(record)
            anomaly_counts[record.anomaly_class] += 1
    total = len(rows)
    precision_denominator = candidate_tp + candidate_fp
    recall_denominator = candidate_tp + candidate_fn
    f1_numerator = 2 * candidate_tp
    f1_denominator = 2 * candidate_tp + candidate_fp + candidate_fn
    return EvaluationReport(dataset_version, run_id, (
        _metric("抽取准确率", extraction_num, total, note="整例候选集合精确匹配"),
        _metric("候选抽取精确率", candidate_tp, precision_denominator, note="候选名称集合 micro precision"),
        _metric("候选抽取召回率", candidate_tp, recall_denominator, note="候选名称集合 micro recall"),
        MetricValue("候选抽取 F1", round(f1_numerator / f1_denominator if f1_denominator else 0.0, 4),
                    f1_denominator, f1_numerator, "ratio", "候选名称集合 micro F1"),
        _metric("引用与证据可追溯覆盖率", citation_num, total, note="期望引用集合被观测引用覆盖"),
        _metric("推荐质量", recommendation_num, total, note="推荐结果与脱敏金标准一致"),
        _metric("失败率", len(failures), total, note="包含已登记失败的案例比例"),
    ), tuple(failures), dict(anomaly_counts))


def evaluate_run(cases: Iterable[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """JSON-friendly facade used by scripts and API/demo callers."""
    return evaluate_cases(cases, **kwargs).to_dict()


__all__ = ["evaluate_cases", "evaluate_run"]
