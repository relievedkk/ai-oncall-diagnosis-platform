"""Frozen label-free preparation. Answer access belongs exclusively to heldout_score."""

import platform
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from app.evaluation.evidence_inventory import inventory_case
from app.evaluation.heldout_dataset import (
    DATA_FILES,
    SOURCE_REVISION,
    digest,
    file_sha,
    read_json,
    safe_child,
)
from app.evaluation.rcaeval import Pilot
from app.evaluation.single_pass_hypotheses import collect_evidence, rank_hypotheses
from app.evaluation.single_pass_packet import build_packet
from app.evaluation.single_pass_runner import source_snapshot
from app.evaluation.single_pass_storage import atomic_json_write


def source_identity():
    root = Path(__file__).resolve().parents[2]
    return {
        "app_sha256": source_snapshot(),
        "lock_sha256": file_sha(root / "uv.lock"),
        "project_sha256": file_sha(root / "pyproject.toml"),
        "evaluation_dependencies_sha256": file_sha(root / "requirements-rcaeval.txt"),
        "python": platform.python_version(),
        "libraries": {p: version(p) for p in ("pandas", "pyarrow", "pydantic", "langchain-openai")},
    }


def verify_tasks(tasks, *, expected_count=36):
    tasks = Path(tasks).resolve(strict=True)
    manifest = read_json(safe_child(tasks, "manifest.json"))
    ids = [f"case-{n:03}" for n in range(1, expected_count + 1)]
    if (
        manifest.get("source_revision") != SOURCE_REVISION
        or manifest.get("schema_version") != 1
        or manifest.get("cases") != [{"case_id": cid, "case": cid} for cid in ids]
    ):
        raise ValueError("dataset_manifest_mismatch")
    files = manifest.get("files", {})
    allowed = {f"{cid}/{name}" for cid in ids for name in DATA_FILES}
    required = {f"{cid}/{name}" for cid in ids for name in ("metrics.parquet", "inject_time.txt")}
    present = {p.relative_to(tasks).as_posix() for p in tasks.rglob("*") if p.is_file()}
    if (
        not required <= set(files)
        or not set(files) <= allowed
        or present != {*files, "manifest.json"}
    ):
        raise ValueError("unexpected_task_files")
    for relative, expected in files.items():
        if file_sha(safe_child(tasks, relative)) != expected:
            raise ValueError("dataset_file_changed")
    if not all(
        isinstance(manifest.get(k), str) and len(manifest[k]) == 64
        for k in ("dataset_id", "answers_sha256")
    ):
        raise ValueError("dataset_identity_missing")
    contents = manifest.get("content_sha256", {})
    if (
        set(contents) != set(ids)
        or digest([SOURCE_REVISION, sorted(contents.values())]) != manifest["dataset_id"]
    ):
        raise ValueError("dataset_content_identity_mismatch")
    return manifest


def _claim_dataset(tasks, run_dir, public):
    from app.evaluation.heldout_registry import claim

    claim(public, run_dir)


def prepare_run(tasks, output, *, expected_count=36, progress=None):
    tasks, output = Path(tasks).resolve(strict=True), Path(output).resolve()
    public = verify_tasks(tasks, expected_count=expected_count)
    identity = source_identity()
    if public.get("preparation_source_identity") != identity:
        raise ValueError("preparation_source_changed")
    if output.exists():
        raise ValueError("run_destination_exists")
    _claim_dataset(tasks, output, public)
    output.mkdir(parents=True)
    pilot = Pilot(tasks)
    artifacts, statuses = {}, {}
    for case_id in sorted(pilot.ids):
        if progress:
            progress(case_id)
        try:
            view = pilot.open(case_id)
            evidence = collect_evidence(view, telemetry_profile="rcaeval-re2")
            hypotheses = rank_hypotheses(evidence)
            inventory = inventory_case(view, evidence)
            packet = build_packet(evidence, hypotheses)
            best = hypotheses[0] if hypotheses else None
            baseline = {
                "case_id": case_id,
                "status": "candidate" if best else "insufficient_evidence",
                "proposal": {
                    "service": best.service if best else None,
                    "fault": best.fault if best else "unknown",
                },
                "top5": [{"service": h.service, "fault": h.fault} for h in hypotheses],
                "strict_eligible": bool(best and best.eligible),
                "model_calls": 0,
                "measurement": "local_ranking",
                "sources": {k: v["status"] for k, v in inventory["sources"].items()},
            }
            values = {"packet": asdict(packet), "inventory": inventory, "baseline": baseline}
            statuses[case_id] = "prepared"
        except Exception:
            # Raw exception strings can contain source paths, telemetry or labels.
            values = {
                "baseline": {
                    "case_id": case_id,
                    "status": "error",
                    "failure_code": "telemetry_or_packet_invalid",
                    "model_calls": 0,
                    "measurement": "local_ranking",
                    "proposal": {},
                    "top5": [],
                }
            }
            statuses[case_id] = "error"
        for kind, value in values.items():
            name = f"{case_id}-{kind}.json"
            atomic_json_write(output / name, value)
            artifacts[name] = file_sha(output / name)
    if (
        source_identity() != identity
        or verify_tasks(tasks, expected_count=expected_count) != public
    ):
        raise ValueError("inputs_changed_during_preflight")
    run = {
        "schema_version": 1,
        "dataset_id": public["dataset_id"],
        "tasks": str(tasks),
        "manifest_sha256": file_sha(tasks / "manifest.json"),
        "answers_sha256": public["answers_sha256"],
        "source_identity": identity,
        "case_ids": sorted(pilot.ids),
        "case_count": len(pilot.ids),
        "artifacts": artifacts,
        "statuses": statuses,
        "model_calls": 0,
        "passed": all(s == "prepared" for s in statuses.values()),
        "profile": "rcaeval-re2",
        "max_request_bytes": 16384,
    }
    run["run_sha256"] = digest(run)
    atomic_json_write(output / "run.json", run)
    return run


def verify_run(output):
    output = Path(output).resolve(strict=True)
    run = read_json(output / "run.json")
    if digest({k: v for k, v in run.items() if k != "run_sha256"}) != run.get("run_sha256"):
        raise ValueError("run_manifest_changed")
    if run["source_identity"] != source_identity():
        raise ValueError("run_source_changed")
    tasks = Path(run["tasks"]).resolve(strict=True)
    public = verify_tasks(tasks, expected_count=run["case_count"])
    if file_sha(tasks / "manifest.json") != run["manifest_sha256"]:
        raise ValueError("run_dataset_changed")
    from app.evaluation.heldout_registry import binding

    if Path(binding(public["dataset_id"])["run"]).resolve() != output:
        raise ValueError("dataset_binding_mismatch")
    for relative, expected in run["artifacts"].items():
        if file_sha(safe_child(output, relative)) != expected:
            raise ValueError("run_artifact_changed")
    return run


def finalize_run(output, *, mode):
    with batch_guard(output) as run:
        return finalize_locked(output, run, mode=mode)


def batch_guard(output):
    from contextlib import contextmanager

    from app.evaluation.heldout_registry import exclusive

    @contextmanager
    def locked():
        run = read_json(Path(output) / "run.json")
        with exclusive(run["dataset_id"]):
            yield verify_run(output)

    return locked()


def finalize_locked(output, run, *, mode):
    from app.evaluation.heldout_registry import retire

    output = Path(output).resolve(strict=True)
    if mode not in {"offline-only", "live"}:
        raise ValueError("invalid_finalization_mode")
    existing = output / "finalized.json"
    if existing.exists():
        final = read_json(existing)
        if final.get("run_sha256") != run["run_sha256"] or final.get("mode") != mode:
            raise ValueError("finalization_mismatch")
        retire(run["dataset_id"], final["final_sha256"])
        return final
    files = dict(run["artifacts"])
    if mode == "live":
        files["live-config.json"] = file_sha(safe_child(output, "live-config.json"))
        for case_id in run["case_ids"]:
            name = f"{case_id}-prediction.json"
            prediction = read_json(safe_child(output, name))
            if prediction.get("case_id") != case_id:
                raise ValueError("prediction_case_mismatch")
            files[name] = file_sha(output / name)
            timing = f"{case_id}-timing.json"
            if (output / timing).exists():
                files[timing] = file_sha(safe_child(output, timing))
    final = {"run_sha256": run["run_sha256"], "mode": mode, "artifacts": files}
    final["final_sha256"] = digest(final)
    atomic_json_write(existing, final)
    retire(run["dataset_id"], final["final_sha256"])
    return final
