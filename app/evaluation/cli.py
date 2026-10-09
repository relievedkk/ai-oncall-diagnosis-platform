"""Run diagnosis quality gates against saved reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.evaluation.diagnosis import DiagnosisCase, evaluate_suite


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate saved AIOps diagnosis reports")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    cases_data = json.loads(args.cases.read_text(encoding="utf-8"))
    results_data = json.loads(args.results.read_text(encoding="utf-8"))
    cases = [DiagnosisCase.from_dict(item) for item in cases_data]
    results = {str(item["case_id"]): item for item in results_data}
    summary = evaluate_suite(cases, results)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
