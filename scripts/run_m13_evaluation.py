"""Run the checked-in M13 fixture set and print a stable JSON report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.evaluation_monitoring import evaluate_run


DEFAULT_DATASET = ROOT / "tests" / "fixtures" / "m13_evaluation_set.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run M13 offline evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--verify-sources", action="store_true", help="verify external input files by SHA-256")
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    source_checks = []
    if args.verify_sources:
        for case in payload["inputs"]:
            path = Path(case["source_path"])
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            source_checks.append({"case_id": case["case_id"], "exists": path.is_file(),
                                  "hash_matches": actual == case["source_sha256"]})
    report = evaluate_run(payload["inputs"], run_id="m13-cli", dataset_version=payload["dataset_version"])
    report["source_checks"] = source_checks
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
