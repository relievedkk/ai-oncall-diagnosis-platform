"""Explicit, bounded all54 measurement using the revision-bound V5.1 diagnosis engine."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from app.agent.aiops.single_pass import run_single_pass
from app.core.single_pass import REVISION, canonical
from app.evaluation.rcaeval import Pilot
from app.evaluation.single_pass_hypotheses import collect_evidence, rank_hypotheses
from app.evaluation.single_pass_journal import AttemptJournal
from app.evaluation.single_pass_packet import build_packet
from app.evaluation.single_pass_preflight import cached_packets, obtain_preflight
from app.evaluation.single_pass_runner import (
    DEVELOPMENT_ANSWERS,
    DEVELOPMENT_ROOT,
    _write,
    attach_scores,
    blocked_case,
    source_snapshot,
    summarize_runs,
)
from app.evaluation.single_pass_storage import PersistenceError

ALL_ROOT = Path(".runtime/rcaeval/heldout-54-v1/tasks")
ALL_ANSWERS = Path(".runtime/rcaeval/heldout-54-v1-sealed/answers.parquet")
SMOKE_IDS = frozenset(
    "case-001 case-012 case-013 case-014 case-019 case-024 case-026 case-032".split()
)


def protocol_gate(runs, case_ids):
    indexed = {r["case_id"]: r for r in runs}
    return bool(case_ids) and all(
        cid in indexed
        and indexed[cid].get("status") != "error"
        and indexed[cid].get("model_calls") == 1
        and indexed[cid].get("usage", {}).get("observed") is True
        and indexed[cid].get("evidence_binding_origin") == "server_bound_candidate"
        for cid in case_ids
    )


def validate_case_ids(ids):
    if set(ids) != {f"case-{i:03}" for i in range(1, 55)}:
        raise ValueError("all54_scope_mismatch")


def approve_execution(audit, *, reviewed_sha, acknowledge):
    digest = hashlib.sha256(canonical(audit).encode()).hexdigest()
    if audit.get("audited") != 18 or audit.get("errors") != 0:
        raise ValueError("development_audit_incomplete")
    if reviewed_sha != digest:
        raise ValueError("reviewed_audit_hash_mismatch")
    low_feasibility = not audit.get("paid_gate_passed", False)
    if low_feasibility and not acknowledge:
        raise ValueError("low_feasibility_requires_explicit_acknowledgment")
    return low_feasibility


async def evaluate_cases(
    pilot,
    *,
    output_dir,
    journal,
    experiment_id,
    config,
    model_factory,
    telemetry_profile,
    timeout_seconds,
    resume=False,
    case_ids=None,
    prepared_packets=None,
):
    """Persist each validated prediction before loading its label; never retry unknowns."""
    output_dir = Path(output_dir)
    runs = []
    expected = len(pilot.ids)
    selected = set(pilot.ids) if case_ids is None else set(case_ids)
    if not selected <= set(pilot.ids):
        raise ValueError("evaluation_scope_mismatch")
    for case_id in sorted(selected):
        availability = {"logs": "unknown", "traces": "unknown"}
        stage = "open_dataset"
        try:
            if prepared_packets is None:
                view = pilot.open(case_id)
                availability = {
                    "logs": "present" if not view.l.empty else "not_available",
                    "traces": "present" if not view.t.empty else "not_available",
                }
                stage = "extract_evidence"
                evidence = collect_evidence(view, telemetry_profile=telemetry_profile)
                stage = "build_packet"
                packet = build_packet(evidence, rank_hypotheses(evidence))
            else:
                stage = "load_preflight_packet"
                packet = prepared_packets[case_id]
                availability = {
                    f["source"]: f["data"]["status"]
                    for f in packet.payload["quality"]["sources"]
                    if f["source"] in {"logs", "traces"}
                }
            stage = "execute_or_resume"
            result = await run_single_pass(
                packet,
                journal=journal,
                experiment_id=experiment_id,
                config=config,
                model_factory=lambda packet=packet: model_factory(packet),
                resume=resume,
                timeout_seconds=timeout_seconds,
            )
        except PersistenceError:
            raise  # Stop immediately; a paid result must not become a generic miss.
        except ValueError as exc:
            if str(exc) in {"configuration_mismatch", "explicit_resume_required"}:
                raise
            result = blocked_case(journal, experiment_id, case_id, "attempt_or_evidence_blocked")
            allowed = {
                "log_timestamps_unusable",
                "log_row_accounting_mismatch",
                "log_fact_extraction_empty",
                "evidence_budget_exceeded",
                "hidden_retries_forbidden",
                "attempt_already_reserved",
            }
            result.setdefault("failure_stage", stage)
            result.setdefault(
                "failure_detail_code", str(exc) if str(exc) in allowed else "preparation_failed"
            )
        except Exception:
            result = blocked_case(journal, experiment_id, case_id, "attempt_or_evidence_blocked")
            result.setdefault("failure_stage", stage)
            result.setdefault("failure_detail_code", "preparation_failed")
        result = {**result, "revision": REVISION, "protocol_version": 5}
        prediction_path = output_dir / f"{case_id}-prediction.json"
        if not resume or not prediction_path.exists():
            _write(prediction_path, result)
        # This is the first evaluation-label access for this case.
        truth = pilot.truth(case_id)
        attach_scores(result, truth)
        result["ground_truth"] = {
            "service": truth["root_cause_service"],
            "fault": truth["fault"],
            "system": str(truth.get("system", "unknown")),
        }
        result["availability"] = availability
        runs.append(result)
        _write(output_dir / f"{case_id}-single-pass.json", result)
        summary = summarize_runs(runs)
        summary.update(
            revision=REVISION,
            evaluation_set="all54",
            expected_case_count=expected,
            complete=len(runs) == expected,
            smoke_gate_passed=protocol_gate(runs, set(config.get("smoke_case_ids", [])))
            if config.get("smoke_case_ids")
            else None,
            limitation="Mixed previously exposed/development cases; not an independent blind benchmark. Unknowns and errors count as misses.",
        )
        _write(output_dir / "summary.json", summary)
        print(
            canonical(
                {
                    "completed": len(runs),
                    "total": expected,
                    "case_id": case_id,
                    "status": result["status"],
                    "score": result["score"],
                    "model_calls": result["model_calls"],
                }
            ),
            flush=True,
        )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-version", type=int, choices=[5], default=5)
    parser.add_argument("--mode", choices=["validate", "live"], default="validate")
    parser.add_argument("--data-dir", type=Path, default=ALL_ROOT)
    parser.add_argument("--answers-file", type=Path, default=ALL_ANSWERS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--accept-low-feasibility", action="store_true")
    parser.add_argument("--reviewed-audit-sha256")
    parser.add_argument("--experiment-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--phase", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--preflight-file", type=Path)
    parser.add_argument("--max-calls", type=int, choices=[1], default=1)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--telemetry-profile", choices=["rcaeval-re2"], default="rcaeval-re2")
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be finite and in (0,300]")
    if (
        args.data_dir.resolve() != ALL_ROOT.resolve()
        or args.answers_file.resolve() != ALL_ANSWERS.resolve()
    ):
        parser.error("all54 only permits the explicitly selected known dataset and answer source")
    if args.mode == "live" and (
        not args.allow_live or not args.experiment_id or not args.reviewed_audit_sha256
    ):
        parser.error("live requires --allow-live, --experiment-id and --reviewed-audit-sha256")
    if args.resume and args.mode != "live":
        parser.error("resume is only supported for live results")
    if args.mode == "live" and args.phase == "full" and not args.resume:
        parser.error("full requires --resume of a passed smoke experiment")
    manifest_path = args.output_dir / "experiment.json"
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        if not args.resume or not manifest_path.is_file():
            parser.error("output-dir must be empty; resume requires an experiment manifest")
    pilot = Pilot(args.data_dir, answers_file=args.answers_file)
    validate_case_ids(pilot.ids)
    if not pilot.dataset.startswith("RCAEval RE2"):
        parser.error("all54 requires explicit RCAEval RE2 provenance")
    development = Pilot(DEVELOPMENT_ROOT, answers_file=DEVELOPMENT_ANSWERS)
    try:
        package = obtain_preflight(
            development, pilot, telemetry_profile=args.telemetry_profile, cached=args.preflight_file
        )
    except (ValueError, KeyError, TypeError):
        parser.error("preflight_source_input_or_format_mismatch")
    audit = package["audit"]
    audit_sha = package["audit_sha256"]
    validation = package["telemetry_validation"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.resume:
        _write(args.output_dir / "development-audit.json", audit)
        _write(args.output_dir / "preflight.json", package)
    print(canonical({k: v for k, v in audit.items() if k != "cases"}), flush=True)
    print("audit_sha256=" + audit_sha, flush=True)
    if not validation.get("passed") or validation.get("case_count") != 54:
        print("Full telemetry/packet validation failed; 0 model calls.", flush=True)
        return 1
    if args.mode == "validate":
        return 0 if audit["audited"] == 18 and not audit["errors"] else 1
    try:
        low_feasibility = approve_execution(
            audit,
            reviewed_sha=args.reviewed_audit_sha256,
            acknowledge=args.accept_low_feasibility,
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        prepared_packets = cached_packets(validation, pilot.ids)
    except (ValueError, KeyError, TypeError):
        parser.error("cached_packet_invalid; 0 model calls")
    config = {
        "protocol": 5,
        "revision": REVISION,
        "code_revision": source_snapshot(),
        "model": "qwen-max",
        "temperature": 0,
        "max_output_tokens": 384,
        "max_bytes": 16384,
        "max_calls": 1,
        "timeout_seconds": args.timeout_seconds,
        "audit_sha256": audit_sha,
        "preflight_context": package["context"],
        "telemetry_profile": args.telemetry_profile,
        "low_feasibility_acknowledged": low_feasibility,
        "smoke_case_ids": sorted(SMOKE_IDS),
        "manifest_sha256": hashlib.sha256(
            (args.data_dir / "manifest.json").read_bytes()
        ).hexdigest(),
        "answers_sha256": hashlib.sha256(args.answers_file.read_bytes()).hexdigest(),
    }
    experiment = {
        "experiment_id": args.experiment_id,
        "config": config,
        "data_dir": str(args.data_dir.resolve()),
        "case_ids": sorted(pilot.ids),
        "answers_file": str(args.answers_file.resolve()),
        "output_dir": str(args.output_dir.resolve()),
    }
    if manifest_path.exists():
        if json.loads(manifest_path.read_text("utf-8")) != experiment:
            parser.error("configuration_mismatch: resume cannot alter experiment settings")
    journal = AttemptJournal(Path(".runtime/rcaeval/single-pass-attempts.sqlite"))
    try:
        journal.bind_experiment(
            args.experiment_id, hashlib.sha256(canonical(experiment).encode()).hexdigest()
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not manifest_path.exists():
        _write(manifest_path, experiment)
    if args.phase == "full":
        prior = []
        for cid in SMOKE_IDS:
            row = journal.load(args.experiment_id, cid)
            if row and row["state"] == "complete":
                prior.append(journal.cached(row))
        if not protocol_gate(prior, SMOKE_IDS):
            parser.error("smoke_protocol_gate_not_passed; remaining paid cases are blocked")
    # No provider configuration is imported until scope, audit and manifest are verified.
    from app.core.llm_factory import llm_factory

    summary = asyncio.run(
        evaluate_cases(
            pilot,
            output_dir=args.output_dir,
            journal=journal,
            experiment_id=args.experiment_id,
            config=config,
            model_factory=lambda _: llm_factory.create_chat_model(
                model="qwen-max",
                temperature=0,
                streaming=False,
            ).model_copy(update={"max_tokens": 384}),
            telemetry_profile=args.telemetry_profile,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
            case_ids=SMOKE_IDS if args.phase == "smoke" else None,
            prepared_packets=prepared_packets,
        )
    )
    print(canonical({k: v for k, v in summary.items() if k != "runs"}), flush=True)
    return 0
