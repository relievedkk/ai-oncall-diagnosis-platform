"""Offline RCAEval scoring; reference validity never establishes causality."""

import argparse
import json
from pathlib import Path

from app.core.evidence import valid_evidence


def assess_run(run):
    prediction, truth = run.get("prediction", {}), run["ground_truth"]
    report = run.get("diagnosis_result") or {}
    errors = run.get("errors") or []
    outcome = report.get("outcome")
    failure_category = run.get("failure_category")
    timed_out = (
        failure_category == "timeout"
        or any(str(error).startswith("TimeoutError") for error in errors)
        or report.get("termination_reason") == "comparison_timeout"
    )
    budget = outcome == "budget_exhausted" or "ComparisonBudgetExceeded" in errors
    execution_ok = (
        failure_category is None
        and not errors
        and outcome not in {"dependency_failed", "budget_exhausted"}
    )
    service = prediction.get("service") == truth["root_cause_service"]
    fault = prediction.get("fault") == truth["fault"]
    refs = [ref for item in report.get("observations", []) for ref in item.get("evidence_refs", [])]
    refs += [
        ref
        for item in report.get("hypotheses", [])
        for key in ("supporting_refs", "opposing_refs")
        for ref in item.get(key, [])
    ]
    refs += report.get("cause_evidence_refs", [])
    refs += [
        ref
        for item in report.get("candidate_comparisons", [])
        for key in ("supporting_refs", "opposing_refs")
        for ref in item.get(key, [])
    ]
    if "valid_evidence_refs" in run:
        valid_refs = set(run.get("valid_evidence_refs") or [])
        references = (
            all(ref in valid_refs for ref in refs) and not report.get("validation_issues")
            if refs
            else None
        )
    else:
        evidence = {
            e.get("evidence_ref"): e for e in run.get("evidence", []) if e.get("evidence_ref")
        }
        references = (
            (
                all(ref in evidence and valid_evidence(evidence[ref]) for ref in refs)
                and not report.get("validation_issues")
            )
            if refs
            else None
        )
    termination = report.get("termination_reason") or run.get("termination_reason", "")
    if timed_out:
        execution_category = "timeout"
    elif budget:
        execution_category = "budget_exhausted"
    elif failure_category:
        execution_category = failure_category
    elif not execution_ok:
        execution_category = "dependency_failed"
    else:
        execution_category = "success"
    initial_candidates = run.get("initial_candidates", [])
    expanded_candidates = run.get("expanded_candidates", initial_candidates)
    safe_unknown = execution_ok and (
        not prediction.get("service") or prediction.get("fault") in (None, "", "unknown")
    )
    return {
        "schema_version": 3,
        "execution_ok": execution_ok,
        "execution_category": execution_category,
        "termination_reason": termination,
        "label_match": {
            "service_correct": service,
            "fault_correct": fault,
            "joint_correct": service and fault,
        },
        "service_correct": execution_ok and service,
        "fault_correct": execution_ok and fault,
        "joint_correct": execution_ok and service and fault,
        "service_abstained": not prediction.get("service") and execution_ok,
        "fault_abstained": prediction.get("fault") in (None, "", "unknown") and execution_ok,
        "safe_unknown": safe_unknown,
        "initial_candidate_recall": (
            truth["root_cause_service"] in initial_candidates if initial_candidates else None
        ),
        "expanded_candidate_recall": (
            truth["root_cause_service"] in expanded_candidates if expanded_candidates else None
        ),
        "timed_out": timed_out,
        "budget_exhausted": budget,
        "references_valid": references,
        "referenced_claim_count": len(refs),
        "causal_support": None,
        "review_status": "pending",
        "extraction_faithfulness": None,
        "review_checklist": [
            "Does the extracted fault appear as a causal assertion in the original report?",
            "Do cited observations distinguish the proposed cause from downstream symptoms?",
            "Are competing causes considered and missing evidence disclosed?",
        ],
    }


def summarize(runs):
    output = {}
    for strategy in sorted({r["strategy"] for r in runs}):
        rows = [assess_run(r) for r in runs if r["strategy"] == strategy]
        n = len(rows)
        successful = [row for row in rows if row["execution_ok"]]
        candidate_rows = [row for row in rows if row["initial_candidate_recall"] is not None]
        expanded_rows = [row for row in rows if row["expanded_candidate_recall"] is not None]
        output[strategy] = {
            "runs": n,
            "scoring_version": 3,
            **{
                name: sum(bool(r[key]) for r in rows) / n
                for name, key in (
                    ("service_accuracy", "service_correct"),
                    ("fault_accuracy", "fault_correct"),
                    ("joint_accuracy", "joint_correct"),
                    ("timeout_rate", "timed_out"),
                    ("execution_success_rate", "execution_ok"),
                    ("budget_exhaustion_rate", "budget_exhausted"),
                    ("service_abstention_rate", "service_abstained"),
                    ("fault_abstention_rate", "fault_abstained"),
                )
            },
            "reference_evaluable_runs": sum(r["references_valid"] is not None for r in rows),
            "conditional_service_accuracy": (
                sum(r["label_match"]["service_correct"] for r in successful) / len(successful)
                if successful
                else None
            ),
            "conditional_fault_accuracy": (
                sum(r["label_match"]["fault_correct"] for r in successful) / len(successful)
                if successful
                else None
            ),
            "conditional_joint_accuracy": (
                sum(r["label_match"]["joint_correct"] for r in successful) / len(successful)
                if successful
                else None
            ),
            "initial_candidate_recall": (
                sum(r["initial_candidate_recall"] for r in candidate_rows) / len(candidate_rows)
                if candidate_rows
                else None
            ),
            "expanded_candidate_recall": (
                sum(r["expanded_candidate_recall"] for r in expanded_rows) / len(expanded_rows)
                if expanded_rows
                else None
            ),
            "safe_unknown_rate": sum(r["safe_unknown"] for r in rows) / n,
            "failures_by_category": {
                category: sum(r["execution_category"] == category for r in rows)
                for category in (
                    "timeout",
                    "budget_exhausted",
                    "provider_setup_error",
                    "provider_error",
                    "invalid_initial_bundle",
                    "invalid_model_output",
                    "invalid_evidence_request",
                    "followup_loader_error",
                    "policy_validation_error",
                    "dependency_failed",
                )
            },
            "model_calls": sum(
                (run.get("usage") or {}).get("model_calls", 0)
                for run in runs
                if run["strategy"] == strategy
            ),
            "causal_reviewed_runs": 0,
        }
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.input.resolve() == args.output.resolve():
        parser.error("Choose a new output file; historical results are preserved")
    source = json.loads(args.input.read_text(encoding="utf-8"))
    runs = [{**run, "scoring_v3": assess_run(run)} for run in source["runs"]]
    result = {
        **source,
        "runs": runs,
        "scoring_version": 3,
        "metrics": summarize(runs),
        "original_input": str(args.input),
        "model_calls_for_rescoring": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result["metrics"], indent=2))


if __name__ == "__main__":
    main()
