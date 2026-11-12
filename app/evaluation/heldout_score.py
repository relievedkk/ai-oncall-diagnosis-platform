"""Post-freeze label access and full-denominator metrics, separate from diagnosis."""

import math
from collections import Counter
from pathlib import Path

import pandas as pd

from app.core.single_pass import FAULTS
from app.evaluation.heldout_dataset import digest, file_sha, read_json, safe_child
from app.evaluation.heldout_protocol import batch_guard, finalize_locked
from app.evaluation.single_pass_storage import atomic_json_write


def wilson(correct, n):
    if not n:
        return None
    z = 1.959963984540054
    center = (correct / n + z * z / (2 * n)) / (1 + z * z / n)
    radius = (
        z * math.sqrt(correct / n * (1 - correct / n) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def validate_answers(frame, ids):
    if (
        not {"case_id", "root_cause_service", "fault", "system"} <= set(frame.columns)
        or len(frame) != len(ids)
        or not frame.case_id.is_unique
        or set(frame.case_id) != set(ids)
    ):
        raise ValueError("answer_scope_mismatch")
    if any(
        not isinstance(r.root_cause_service, str)
        or not r.root_cause_service
        or r.fault not in FAULTS
        or r.system not in {"ob", "ss", "tt"}
        for r in frame.itertuples()
    ):
        raise ValueError("invalid_answer")
    return {
        r["case_id"]: {
            "service": r["root_cause_service"],
            "fault": r["fault"],
            "system": r["system"],
        }
        for r in frame.to_dict("records")
    }


def _joint(service, fault, truth):
    return service == truth["service"] and fault == truth["fault"]


def summarize_models(rows):
    n = len(rows)
    counts = Counter()
    states = Counter()
    tokens, observed = [], 0
    for row in rows:
        p, truth = row["prediction"], row["truth"]
        state = p.get("status", "error")
        states[state] += 1
        counts["model_calls"] += p.get("model_calls", 0)
        usage = p.get("usage", {})
        if type(usage.get("total_tokens")) is int and usage.get("observed"):
            tokens.append(usage["total_tokens"])
            observed += 1
        if state not in {"supported", "candidate", "insufficient_evidence"}:
            continue
        proposal = p.get("proposal", {})
        counts["proposal_service"] += proposal.get("service") == truth["service"]
        counts["proposal_fault"] += proposal.get("fault") == truth["fault"]
        counts["proposal_joint"] += _joint(proposal.get("service"), proposal.get("fault"), truth)
        counts["service"] += p.get("service") == truth["service"]
        confirmed = state == "supported" and p.get("confirmed") is True
        counts["confirmed"] += confirmed
        correct = bool(confirmed and _joint(p.get("service"), p.get("fault"), truth))
        counts["confirmed_joint"] += correct
        counts["wrong_confirmations"] += confirmed and not correct
        counts["candidate_joint"] += correct or _joint(
            p.get("candidate_service"), p.get("candidate_fault"), truth
        )
    result = {
        "n": n,
        **dict(states),
        "model_calls": counts["model_calls"],
        "observed_tokens": sum(tokens) if tokens else None,
        "usage_coverage": observed / counts["model_calls"] if counts["model_calls"] else None,
        "wrong_confirmations": counts["wrong_confirmations"],
        "confirmation_coverage": counts["confirmed"] / n if n else None,
        "wrong_confirmation_rate": counts["wrong_confirmations"] / counts["confirmed"]
        if counts["confirmed"]
        else None,
    }
    for key in (
        "proposal_service",
        "proposal_fault",
        "proposal_joint",
        "service",
        "candidate_joint",
        "confirmed_joint",
    ):
        result[key + "_accuracy"] = counts[key] / n if n else None
        result[key + "_wilson95"] = wilson(counts[key], n)
    return result


def summarize_baselines(rows):
    n = len(rows)
    top1 = service = top5 = service3 = 0
    for row in rows:
        p, truth = row["baseline"], row["truth"]
        if p.get("status") == "error":
            continue
        proposal = p["proposal"]
        service += proposal.get("service") == truth["service"]
        top1 += _joint(proposal.get("service"), proposal.get("fault"), truth)
        top5 += any(_joint(h.get("service"), h.get("fault"), truth) for h in p["top5"])
        service3 += truth["service"] in list(dict.fromkeys(h.get("service") for h in p["top5"]))[:3]
    return {
        "n": n,
        "measurement": "local_ranking_not_model_accuracy",
        "service_top1_accuracy": service / n if n else None,
        "joint_top1_accuracy": top1 / n if n else None,
        "joint_top5_recall": top5 / n if n else None,
        "service_top3_recall": service3 / n if n else None,
        "joint_top1_wilson95": wilson(top1, n),
        "joint_top5_wilson95": wilson(top5, n),
    }


def score_run(run_dir, answers_file, *, offline_only=False):
    with batch_guard(run_dir) as run:
        return score_locked(run_dir, run, answers_file, offline_only=offline_only)


def score_locked(run_dir, run, answers_file, *, offline_only=False):
    output = Path(run_dir).resolve(strict=True)
    if file_sha(answers_file) != run["answers_sha256"]:
        raise ValueError("answers_commitment_mismatch")
    final_path = output / "finalized.json"
    if not final_path.exists():
        if not offline_only:
            raise ValueError("run_unfinalized")
        finalize_locked(output, run, mode="offline-only")
    final = read_json(final_path)
    if (
        digest({k: v for k, v in final.items() if k != "final_sha256"}) != final.get("final_sha256")
        or final.get("run_sha256") != run["run_sha256"]
    ):
        raise ValueError("finalization_changed")
    if final["mode"] == "offline-only" and not offline_only:
        raise ValueError("explicit_offline_only_required")
    for name, expected in final["artifacts"].items():
        if file_sha(safe_child(output, name)) != expected:
            raise ValueError("final_artifact_changed")
    expected = (
        {f"{cid}-prediction.json" for cid in run["case_ids"]} if final["mode"] == "live" else set()
    )
    if {p.name for p in output.glob("*-prediction.json")} != expected:
        raise ValueError("prediction_scope_mismatch")
    # First label read. All code/data/predictions and answer commitments are verified above.
    truths = validate_answers(pd.read_parquet(answers_file), run["case_ids"])
    rows = []
    for cid in run["case_ids"]:
        baseline = read_json(output / f"{cid}-baseline.json")
        row = {"case_id": cid, "truth": truths[cid], "baseline": baseline}
        if final["mode"] == "live":
            row["prediction"] = read_json(output / f"{cid}-prediction.json")
        rows.append(row)

    def summarize(subset):
        return {
            "baseline": summarize_baselines(subset),
            "model": summarize_models(subset) if final["mode"] == "live" else None,
        }

    report = {
        "schema_version": 1,
        "dataset_id": run["dataset_id"],
        "run_sha256": run["run_sha256"],
        "final_sha256": final["final_sha256"],
        "retired": True,
        "case_count": run["case_count"],
        "mode": final["mode"],
        **summarize(rows),
        "strata": {},
        "limitation": "Project group holdout, not third-party audit or online accuracy. Proposal, candidate and confirmation differ.",
    }
    durations = []
    for name in final["artifacts"]:
        if name.endswith("-timing.json"):
            value = read_json(output / name).get("attempt_elapsed_seconds")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                durations.append(value)
    durations.sort()
    report["timing"] = {
        "observed_attempts": len(durations),
        "attempt_p95_seconds": durations[math.ceil(0.95 * len(durations)) - 1]
        if durations
        else None,
        "scope": "local_attempt_not_MTTR",
    }
    for dimension in ("system", "fault", "logs", "traces"):

        def group(row, dimension=dimension):
            return (
                row["truth"][dimension]
                if dimension in {"system", "fault"}
                else row["baseline"].get("sources", {}).get(dimension, "unknown")
            )

        report["strata"][dimension] = {
            value: summarize([r for r in rows if group(r) == value])
            for value in sorted({group(r) for r in rows})
        }
    atomic_json_write(output / "scores.json", report)
    text = (
        "# Grouped holdout results\n\n"
        f"Mode: {report['mode']}; fixed denominator: {report['case_count']}.\n\n"
        f"Local ranking joint Top1: {report['baseline']['joint_top1_accuracy']}; "
        f"joint Top5 recall: {report['baseline']['joint_top5_recall']}.\n\n"
        f"Model: {report['model'] if report['model'] is not None else 'not measured'}.\n\n"
        "See scores.json for source/system/fault strata and Wilson intervals.\n"
    )
    (output / "report.md").write_text(text, encoding="utf-8")
    return report
