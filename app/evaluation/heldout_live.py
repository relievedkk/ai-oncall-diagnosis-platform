"""Explicitly selected live measurement; never imports or reads test answers."""

from pathlib import Path
from time import perf_counter

from app.evaluation.heldout_dataset import digest, read_json
from app.evaluation.heldout_protocol import batch_guard, finalize_locked, verify_run
from app.evaluation.single_pass_storage import atomic_json_write


async def run_live(
    output,
    *,
    model_factory,
    model_config,
    allow_live=False,
    reviewed_sha=None,
    phase="smoke",
    resume=False,
):
    if not allow_live:
        raise ValueError("explicit_live_required")
    with batch_guard(output) as run:
        return await execute_locked(
            output,
            run,
            model_factory=model_factory,
            model_config=model_config,
            reviewed_sha=reviewed_sha,
            phase=phase,
            resume=resume,
        )


async def execute_locked(output, run, *, model_factory, model_config, reviewed_sha, phase, resume):
    from app.evaluation.heldout_registry import binding, journal_path

    output = Path(output).resolve(strict=True)
    if (output / "finalized.json").exists() or binding(run["dataset_id"])["retired"]:
        raise ValueError("run_already_finalized")
    if reviewed_sha != run["run_sha256"] or not run["passed"]:
        raise ValueError("reviewed_preflight_mismatch")
    if phase not in {"smoke", "full"}:
        raise ValueError("invalid_live_phase")
    # Delayed imports keep provider configuration out of every zero-cost command.
    from app.agent.aiops.single_pass import configuration_fingerprint, run_single_pass
    from app.evaluation.single_pass_journal import AttemptJournal
    from app.evaluation.single_pass_packet import Packet

    config = {
        "run_sha256": run["run_sha256"],
        "model": model_config,
        "timeout_seconds": 180,
        "max_calls_per_case": 1,
    }
    config_path = output / "live-config.json"
    if config_path.exists():
        if read_json(config_path) != config:
            raise ValueError("configuration_mismatch")
        if not resume:
            raise ValueError("explicit_resume_required")
    else:
        if phase == "full":
            raise ValueError("smoke_required")
        atomic_json_write(config_path, config)
    journal = AttemptJournal(journal_path())
    experiment_id = run["dataset_id"]
    journal.bind_experiment(experiment_id, digest(config))
    smoke = run["case_ids"][:3]

    def passed(value):
        return (
            value.get("status") not in {"error", "not_run"}
            and value.get("usage", {}).get("observed") is True
            and value.get("evidence_binding_origin") == "server_bound_candidate"
        )

    if phase == "full" and not all(
        (output / f"{cid}-prediction.json").is_file()
        and passed(read_json(output / f"{cid}-prediction.json"))
        for cid in smoke
    ):
        raise ValueError("smoke_gate_failed")
    selected = smoke if phase == "smoke" else run["case_ids"]
    stopped = False
    for case_id in selected:
        path = output / f"{case_id}-prediction.json"
        packet = Packet(**read_json(output / f"{case_id}-packet.json"))
        row = journal.load(experiment_id, case_id)
        if row and resume:
            row = journal.recover(experiment_id, case_id, configuration_fingerprint(packet, config))
        receipt = journal.cached(row) if row and row.get("result") else None
        if path.exists():
            value = read_json(path)
            if row is None or value != receipt:
                raise ValueError("prediction_journal_mismatch")
        elif receipt is not None:
            value = receipt
            atomic_json_write(path, value)
        else:
            started = perf_counter()
            try:
                value = await run_single_pass(
                    packet,
                    journal=journal,
                    experiment_id=experiment_id,
                    config=config,
                    model_factory=lambda packet=packet: model_factory(packet),
                    resume=resume,
                    timeout_seconds=180,
                )
            except ValueError as exc:
                if str(exc) != "attempt_already_reserved":
                    raise
                value = {
                    "case_id": case_id,
                    "status": "error",
                    "error": "reserved_unknown",
                    "model_calls": 1,
                    "packet_sha256": packet.sha256,
                }
            # Reuse preserves the original stored result, not transient resume flags.
            current = journal.load(experiment_id, case_id)
            if current and current.get("result"):
                value = journal.cached(current)
            if row is None:
                atomic_json_write(
                    output / f"{case_id}-timing.json",
                    {
                        "case_id": case_id,
                        "attempt_elapsed_seconds": perf_counter() - started,
                        "scope": "local_attempt_including_preparation_and_transport_not_end_to_end_MTTR",
                    },
                )
            atomic_json_write(path, value)
        if not passed(value):
            stopped = True
            break
    if stopped:
        for case_id in run["case_ids"]:
            path = output / f"{case_id}-prediction.json"
            if not path.exists():
                atomic_json_write(path, {"case_id": case_id, "status": "not_run", "model_calls": 0})
    if stopped or phase == "full":
        verify_run(output)
        # Bind the model configuration to final artifact verification too.
        final = finalize_locked(output, run, mode="live")
        return {"complete": not stopped, "stopped": stopped, "final_sha256": final["final_sha256"]}
    return {"complete": False, "smoke_passed": True, "model_cases": len(selected)}
