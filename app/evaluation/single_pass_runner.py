"""Single-only protocol v5. Default validation is local and never creates a model."""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path

from app.core.single_pass import REVISION, canonical
from app.evaluation.single_pass_hypotheses import collect_evidence, rank_hypotheses
from app.evaluation.single_pass_packet import build_packet

DEVELOPMENT_ROOT = Path(".runtime/rcaeval/heldout-18-v1/tasks")
DEVELOPMENT_ANSWERS = Path(".runtime/rcaeval/heldout-18-v1-sealed/answers.parquet")


def feasibility_gate(audit):
    return (
        audit.get("case_count") == 18
        and audit.get("audited") == 18
        and audit.get("candidate_pair_recall", 0) >= 17
        and audit.get("eligible_correct_pairs", 0) >= 17
        and audit.get("packet_passed") == 18
        and audit.get("errors") == 0
    )


def source_snapshot(root=None):
    """Freeze actual source bytes, including local edits; do not execute git from PATH."""
    root = Path(root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    entries = []
    for path in sorted(root.rglob("*.py")):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("source_path_escapes_snapshot")
        entries.append(
            [path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]
        )
    return "sha256:" + hashlib.sha256(canonical(entries).encode()).hexdigest()


def _freeze(view, *, perturb=True, telemetry_profile=None):
    evidence = collect_evidence(view, telemetry_profile=telemetry_profile)
    hypotheses = rank_hypotheses(evidence)
    selected = [h.model_dump() for h in hypotheses]
    # All services' permissions are frozen without labels; bounded top5 recall is scored separately.
    catalog = [
        h.model_dump()
        for s in evidence.services
        for h in rank_hypotheses(evidence.model_copy(update={"services": (s,)}), limit=6)
    ]
    packet, packet_error = None, None
    try:
        packet = build_packet(evidence, hypotheses)
    except ValueError as exc:
        packet_error = (
            "evidence_budget_exceeded"
            if str(exc) == "evidence_budget_exceeded"
            else "packet_invalid"
        )
    ablations = {}
    for name, sources in (
        ("metrics", {"metrics"}),
        ("metrics_logs", {"metrics", "logs"}),
        ("all_sources", {"metrics", "logs", "traces"}),
    ):
        subset = evidence.model_copy(
            update={"facts": tuple(f for f in evidence.facts if f.source in sources)}
        )
        ablations[name] = [h.model_dump() for h in rank_hypotheses(subset)]
    perturbations = []
    if perturb:
        for shift in (-60, -30, 30, 60):
            try:
                perturbed = collect_evidence(
                    view, incident=view.incident + shift, telemetry_profile=telemetry_profile
                )
                candidates = rank_hypotheses(perturbed)
                perturbations.append(
                    {"shift_seconds": shift, "hypotheses": [h.model_dump() for h in candidates]}
                )
            except (ValueError, TypeError):
                perturbations.append(
                    {"shift_seconds": shift, "error": "invalid_perturbed_window", "hypotheses": []}
                )
    return (
        evidence,
        hypotheses,
        packet,
        {
            "selected": selected,
            "catalog": catalog,
            "packet_error": packet_error,
            "ablations": ablations,
            "time_perturbations": perturbations,
        },
    )


def audit_development(pilot, *, scope, progress=None, telemetry_profile=None):
    from app.evaluation.rcaeval_v4 import DevelopmentScope

    if (
        not isinstance(scope, DevelopmentScope)
        or not 1 <= len(pilot.ids) <= 18
        or not set(pilot.ids).issubset(scope.case_ids)
        or Path(pilot.answers_file).resolve() != scope.answers_file
    ):
        raise ValueError("unapproved_development_scope")
    rows = []
    for case_id in pilot.ids:
        if progress:
            progress(case_id)
        try:
            view = pilot.open(case_id)
            evidence, hypotheses, packet, frozen = _freeze(
                view, telemetry_profile=telemetry_profile
            )
            # All features, hypotheses, ablations, time perturbations and packet are frozen first.
            truth = pilot.truth(case_id)
            root, fault = truth["root_cause_service"], truth["fault"]

            def matches(h, root=root, fault=fault):
                return h["service"] == root and h["fault"] == fault

            actual = next((h for h in frozen["catalog"] if matches(h)), None)
            row = {
                "case_id": case_id,
                "ground_truth": {"service": root, "fault": fault},
                "audited": True,
                "candidate_service_recalled": root in {h.service for h in hypotheses},
                "candidate_pair_recalled": any(matches(h) for h in frozen["selected"]),
                "eligible_correct_pair": bool(actual and actual["eligible"]),
                "local_top1_correct": bool(frozen["selected"] and matches(frozen["selected"][0])),
                "packet_passed": packet is not None,
                "packet_error": frozen["packet_error"],
                "transport_bytes": packet.transport_bytes if packet else None,
                "packet_sha256": packet.sha256 if packet else None,
                "correct_hypothesis": actual,
                "top5": frozen["selected"],
                "quality": evidence.quality,
                "inventory": [f.model_dump() for f in evidence.facts],
                "window": evidence.window.model_dump(),
                "ablations": {
                    name: {
                        "pair_recalled": any(matches(h) for h in hs),
                        "eligible_correct_pair": any(matches(h) and h["eligible"] for h in hs),
                    }
                    for name, hs in frozen["ablations"].items()
                },
                "time_perturbations": [
                    {
                        "shift_seconds": p["shift_seconds"],
                        "pair_recalled": any(matches(h) for h in p["hypotheses"]),
                        "eligible_correct_pair": any(
                            matches(h) and h["eligible"] for h in p["hypotheses"]
                        ),
                        "error": p.get("error"),
                    }
                    for p in frozen["time_perturbations"]
                ],
            }
            rows.append(row)
        except Exception:
            rows.append(
                {
                    "case_id": case_id,
                    "audited": False,
                    "error": "case_audit_failed",
                    "candidate_pair_recalled": False,
                    "eligible_correct_pair": False,
                    "packet_passed": False,
                }
            )
    audit = {
        "protocol_version": 5,
        "revision": REVISION,
        "model_calls": 0,
        "case_count": len(rows),
        "cases": rows,
        "audited": sum(r["audited"] for r in rows),
        "candidate_pair_recall": sum(r["candidate_pair_recalled"] for r in rows),
        "eligible_correct_pairs": sum(r["eligible_correct_pair"] for r in rows),
        "packet_passed": sum(r["packet_passed"] for r in rows),
        "errors": sum("error" in r for r in rows),
        "manual_review_required": True,
        "limitation": "Development retrieval/permission audit, not model accuracy or causal proof. Sealed36 labels unopened.",
    }
    audit["paid_gate_passed"] = feasibility_gate(audit)
    return audit


def _write(path, value):
    # Atomic output replacement only after output ownership was checked by main.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    tmp.replace(path)


def blocked_case(journal, experiment_id, case_id, error):
    row = journal.load(experiment_id, case_id)
    cached = journal.cached(row) if row and row["result"] else {}
    return {
        "case_id": case_id,
        "status": "error",
        "error": error,
        "model_calls": 1 if row else 0,
        "new_model_calls": 0,
        "usage": cached.get("usage", {"observed": False, "total_tokens": None}),
        "attempt_state": row["state"] if row else "not_reserved",
    }


def summarize_runs(runs):
    n = len(runs)
    correct = sum(r["score"]["joint_correct"] for r in runs)
    p = correct / n if n else 0
    z = 1.959963984540054
    center = (p + z * z / (2 * n)) / (1 + z * z / n) if n else 0
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n) if n else 0

    def metric(rows):
        return {
            "case_count": len(rows),
            "joint_correct": sum(r["score"]["joint_correct"] for r in rows),
        }

    strata = {}
    for field in ("fault", "system", "logs", "traces"):

        def get(row, field=field):
            return row.get(
                "ground_truth" if field in {"fault", "system"} else "availability", {}
            ).get(field, "unknown")

        strata[field] = {
            value: metric([r for r in runs if get(r) == value])
            for value in sorted({get(r) for r in runs})
        }
    return {
        "protocol_version": 5,
        "strategy": "single_pass",
        "case_count": n,
        "joint_correct": correct,
        "joint_accuracy": p,
        "joint_wilson95": [max(0, center - half), min(1, center + half)] if n else None,
        "service_correct": sum(r["score"]["service_correct"] for r in runs),
        "fault_correct": sum(r["score"]["fault_correct"] for r in runs),
        "failed": sum(r["status"] == "error" for r in runs),
        "insufficient_evidence": sum(r["status"] == "insufficient_evidence" for r in runs),
        "incorrect_confirmation": sum(
            r["status"] == "supported" and not r["score"]["joint_correct"] for r in runs
        ),
        "reserved_attempts": sum(r["model_calls"] for r in runs),
        "observed_tokens": sum(r.get("usage", {}).get("total_tokens") or 0 for r in runs),
        "usage_complete": all(r.get("usage", {}).get("observed") for r in runs if r["model_calls"]),
        "strata": strata,
        "runs": runs,
        "limitation": "Development only; unknown/errors remain in the denominator; not business accuracy.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-version", type=int, choices=[5], default=5)
    parser.add_argument("--data-dir", type=Path, default=DEVELOPMENT_ROOT)
    parser.add_argument("--answers-file", type=Path, default=DEVELOPMENT_ANSWERS)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/rcaeval/single-pass-v5"))
    parser.add_argument("--mode", choices=["validate", "live"], default="validate")
    parser.add_argument("--audit-development", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--reviewed-audit-sha256")
    parser.add_argument("--experiment-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-calls", type=int, choices=[1], default=1)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument(
        "--telemetry-profile", choices=["unknown", "rcaeval-re2"], default="rcaeval-re2"
    )
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be finite and in (0,300]")
    if args.mode == "live" and (
        not args.allow_live or not args.experiment_id or not args.reviewed_audit_sha256
    ):
        parser.error("live requires --allow-live, --experiment-id and --reviewed-audit-sha256")
    if args.resume and args.mode != "live":
        parser.error("resume only reuses journaled live results")
    manifest = args.output_dir / "experiment.json"
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        if not args.resume or not manifest.is_file():
            parser.error(
                "output-dir must be empty; explicit resume requires an experiment manifest"
            )
    if args.audit_development or args.mode == "live":
        if (
            args.data_dir.resolve() != DEVELOPMENT_ROOT.resolve()
            or args.answers_file.resolve() != DEVELOPMENT_ANSWERS.resolve()
        ):
            parser.error(
                "v5 development audit/live only permits the known 18-case development source"
            )
    from app.evaluation.rcaeval import Pilot, score

    pilot = Pilot(
        args.data_dir,
        answers_file=args.answers_file if args.audit_development or args.mode == "live" else None,
    )
    if args.telemetry_profile == "rcaeval-re2" and not pilot.dataset.startswith("RCAEval RE2"):
        parser.error(
            "rcaeval-re2 profile requires an explicit RCAEval RE2 dataset; use --telemetry-profile unknown otherwise"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.audit_development or args.mode == "live":
        from app.evaluation.rcaeval_v4 import DevelopmentScope

        audit = audit_development(
            pilot,
            scope=DevelopmentScope(args.answers_file),
            progress=lambda case_id: print(f"Auditing {case_id} (0 model calls)", flush=True),
            telemetry_profile=args.telemetry_profile,
        )
        audit_sha = hashlib.sha256(canonical(audit).encode()).hexdigest()
        _write(args.output_dir / "development-audit.json", audit)
        print(canonical({k: v for k, v in audit.items() if k != "cases"}), flush=True)
        print("audit_sha256=" + audit_sha, flush=True)
        if args.mode == "validate":
            return 0 if audit["audited"] == len(pilot.ids) and not audit["errors"] else 1
        if not audit["paid_gate_passed"] or args.reviewed_audit_sha256 != audit_sha:
            print(
                "Live blocked: feasibility gate or reviewed audit hash not satisfied. 0 model calls.",
                flush=True,
            )
            return 2
    else:
        rows = []
        for case_id in pilot.ids:
            try:
                view = pilot.open(case_id)
                evidence = collect_evidence(view, telemetry_profile=args.telemetry_profile)
                hypotheses = rank_hypotheses(evidence)
                packet = build_packet(evidence, hypotheses)
                rows.append(
                    {
                        "case_id": case_id,
                        "passed": True,
                        "packet_sha256": packet.sha256,
                        "transport_bytes": packet.transport_bytes,
                        "quality": evidence.quality,
                    }
                )
            except Exception:
                rows.append(
                    {"case_id": case_id, "passed": False, "error": "evidence_validation_failed"}
                )
        report = {
            "protocol_version": 5,
            "model_calls": 0,
            "case_count": len(rows),
            "passed": all(r["passed"] for r in rows),
            "cases": rows,
        }
        _write(args.output_dir / "validation.json", report)
        print(canonical(report), flush=True)
        return 0 if report["passed"] else 1

    # Live imports/configuration are unreachable before the zero-model feasibility gate.
    from app.agent.aiops.single_pass import run_single_pass
    from app.core.llm_factory import llm_factory
    from app.evaluation.single_pass_journal import AttemptJournal

    code = source_snapshot()
    config = {
        "protocol": 5,
        "revision": REVISION,
        "code_revision": code,
        "model": "qwen-max",
        "temperature": 0,
        "max_output_tokens": 384,
        "max_bytes": 16384,
        "max_calls": 1,
        "timeout_seconds": args.timeout_seconds,
        "audit_sha256": audit_sha,
        "telemetry_profile": args.telemetry_profile,
    }
    experiment = {
        "experiment_id": args.experiment_id,
        "config": config,
        "data_dir": str(args.data_dir.resolve()),
        "case_ids": sorted(pilot.ids),
    }
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != experiment:
            parser.error("configuration_mismatch: resume cannot alter experiment settings")
    else:
        _write(manifest, experiment)
    journal = AttemptJournal(Path(".runtime/rcaeval/single-pass-attempts.sqlite"))

    async def experiment_run():
        runs = []
        for case_id in pilot.ids:
            availability = {"logs": "unknown", "traces": "unknown"}
            try:
                view = pilot.open(case_id)
                availability = {
                    "logs": "present" if not view.l.empty else "not_available",
                    "traces": "present" if not view.t.empty else "not_available",
                }
                evidence = collect_evidence(view, telemetry_profile=args.telemetry_profile)
                packet = build_packet(evidence, rank_hypotheses(evidence))
                result = await run_single_pass(
                    packet,
                    journal=journal,
                    experiment_id=args.experiment_id,
                    config=config,
                    model_factory=lambda: llm_factory.create_chat_model(
                        model="qwen-max", temperature=0, streaming=False
                    ).model_copy(update={"max_tokens": 384}),
                    resume=args.resume,
                    timeout_seconds=args.timeout_seconds,
                )
            except ValueError:
                result = blocked_case(
                    journal, args.experiment_id, case_id, "attempt_or_evidence_blocked"
                )
            except Exception:
                result = blocked_case(journal, args.experiment_id, case_id, "case_failed")
            truth = pilot.truth(case_id)
            result["score"] = score(result, truth)
            result["ground_truth"] = {
                "service": truth["root_cause_service"],
                "fault": truth["fault"],
                "system": str(truth.get("system", "unknown")),
            }
            result["availability"] = availability
            runs.append(result)
            _write(args.output_dir / f"{case_id}-single-pass.json", result)
        summary = summarize_runs(runs)
        _write(args.output_dir / "summary.json", summary)

    asyncio.run(experiment_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
