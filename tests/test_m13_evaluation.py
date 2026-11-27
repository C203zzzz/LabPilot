import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.services.evaluation_monitoring import evaluate_cases
from backend.services.candidate_service import extract_candidates, pages_from_document
from backend.services.document_ingestion import DocumentIngestionService


ROOT = Path(__file__).parents[1]
DATASET = ROOT / "tests" / "fixtures" / "m13_evaluation_set.json"


def load_fixture():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_fixed_set_reports_required_metrics_failures_and_anomalies():
    fixture = load_fixture()
    report = evaluate_cases(fixture["inputs"], dataset_version=fixture["dataset_version"])
    metrics = {metric.name: metric for metric in report.metrics}
    assert {"抽取准确率", "候选抽取精确率", "候选抽取召回率", "候选抽取 F1",
            "引用与证据可追溯覆盖率", "推荐质量", "失败率"} <= set(metrics)
    assert metrics["抽取准确率"].value == 0.6
    assert metrics["候选抽取召回率"].value == 0.6667
    assert metrics["推荐质量"].value == 1.0
    assert metrics["失败率"].value == 0.4
    assert len(report.failures) == 2
    assert report.anomalies == {"empty_extraction": 1, "ocr_unavailable": 1}


def test_failure_records_are_reproducible_and_source_records_are_hash_pinned():
    fixture = load_fixture()
    assert len(fixture["inputs"]) == 5
    assert all(len(case["source_sha256"]) == 64 for case in fixture["inputs"])
    report = evaluate_cases(fixture["inputs"])
    assert all(failure.reproducible and failure.reproduction for failure in report.failures)
    assert {failure.case_id for failure in report.failures} == {"pdf-spark-empty", "jpg-no-ocr"}


def test_two_real_failures_reproduce_from_hash_pinned_inputs(tmp_path):
    cases = {case["case_id"]: case for case in load_fixture()["inputs"]}
    spark_path = Path(cases["pdf-spark-empty"]["source_path"])
    image_path = Path(cases["jpg-no-ocr"]["source_path"])
    if not spark_path.is_file() or not image_path.is_file():
        pytest.skip("固定 PDF/JPG 仅存在于指定的本地验收环境")
    assert hashlib.sha256(spark_path.read_bytes()).hexdigest() == cases["pdf-spark-empty"]["source_sha256"]
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == cases["jpg-no-ocr"]["source_sha256"]
    service = DocumentIngestionService(tmp_path / "storage")
    spark = service.ingest_file(spark_path)
    assert extract_candidates(pages_from_document(spark)) == []
    image = service.ingest_file(image_path)
    assert image.ocr_status == "not_requested"
    assert image.extracted_text == "" and image.pages == []


def test_empty_set_has_explicit_zero_metrics_without_division_error():
    report = evaluate_cases([])
    assert all(metric.value == 0.0 and metric.denominator == 0 for metric in report.metrics)
    assert report.failures == () and report.anomalies == {}


def test_cli_runner_is_reproducible_json():
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_m13_evaluation.py"), "--dataset", str(DATASET)],
        cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    payload = json.loads(result.stdout)
    assert payload["dataset_version"] == "m13-v1"
    assert payload["run_id"] == "m13-cli"
    assert len(payload["failures"]) == 2


def test_fixed_set_contains_desensitized_task_and_three_to_five_screening_rules():
    fixture = load_fixture()
    task = fixture["procurement_task"]
    assert task["task_id"].startswith("demo-")
    assert 3 <= len(task["screening_rules"]) <= 5
    assert {rule["kind"] for rule in task["screening_rules"]} >= {"hard", "soft"}
