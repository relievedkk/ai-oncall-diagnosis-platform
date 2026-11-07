"""Bounded remaining46 measurement after the user resumed repaired persistence.

Default is preparation only (zero provider requests). The historical8 remain
immutable, including case026 unknown. A new three-case gate protects the repaired
execution path. Input-reuse proof retains the old preflight context verbatim.
"""

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from app.agent.aiops.single_pass import SinglePassChoice, configuration_fingerprint
from app.core.single_pass import REVISION, canonical
from app.evaluation.rcaeval import Pilot
from app.evaluation.single_pass_batch import (
    ALL_ANSWERS,
    ALL_ROOT,
    SMOKE_IDS,
    evaluate_cases,
    protocol_gate,
)
from app.evaluation.single_pass_journal import AttemptJournal
from app.evaluation.single_pass_preflight import cached_packets, input_snapshot, load_package
from app.evaluation.single_pass_runner import (
    DEVELOPMENT_ANSWERS,
    DEVELOPMENT_ROOT,
    _write,
    source_snapshot,
    summarize_runs,
)
from app.evaluation.single_pass_storage import publish_once

PARENT = Path(".runtime/rcaeval/v51-r2-all54-live-20261104")
OUTPUT = Path(".runtime/rcaeval/v51-r2-persistence-remaining46-20261107")
DB = Path(".runtime/rcaeval/single-pass-attempts.sqlite")
OLD_SOURCE = "sha256:1d3bad321edd64117ea298822a418292775c9add951e3bed8fa85060f1b80881"
NEW_SOURCE = "sha256:871dfa644b5d8a9b2d266db58bf1b30c88c8b8dbbc2af6ba8d219ba22f78ee28"
NEW_ID = "v51-r2-persistence-remaining46-20261107"
GATE = frozenset({"case-002", "case-003", "case-004"})


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def verified_parent_summary(path, expected_sha256):
    body = Path(path).read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError("historical_summary_changed")
    return json.loads(body)


def nodes(source, names):
    tree = ast.parse(source)
    selected = {
        n.name: ast.dump(n, include_attributes=False)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names
    }
    if set(selected) != set(names):
        raise ValueError("diagnosis_contract_changed")
    return selected


def attest_algorithms(before, after):
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    allowed = {
        "app/agent/aiops/single_pass.py",
        "app/evaluation/single_pass_journal.py",
        "app/evaluation/single_pass_runner.py",
        "app/evaluation/single_pass_batch.py",
        "app/evaluation/single_pass_storage.py",
    }
    if set(changed) != allowed:
        raise ValueError("source_transition_not_storage_only")
    checks = {
        "app/agent/aiops/single_pass.py": [
            "SinglePassChoice",
            "SinglePassDecision",
            "validate_choice",
            "validate_decision",
            "render_report",
            "configuration_fingerprint",
            "_usage",
        ],
        "app/evaluation/single_pass_runner.py": [
            "_freeze",
            "audit_development",
            "feasibility_gate",
            "attach_scores",
            "summarize_runs",
            "source_snapshot",
        ],
        "app/evaluation/single_pass_batch.py": [
            "protocol_gate",
            "validate_case_ids",
            "approve_execution",
        ],
    }
    for name, functions in checks.items():
        if nodes(before[name], functions) != nodes(after[name], functions):
            raise ValueError("diagnosis_contract_changed")
    return {
        "changed_files": changed,
        "diagnosis_contract_unchanged": True,
        "old_source": OLD_SOURCE,
        "new_source": NEW_SOURCE,
        "file_digests": {
            k: {
                "old": hashlib.sha256(before[k]).hexdigest() if k in before else None,
                "new": hashlib.sha256(after[k]).hexdigest(),
            }
            for k in changed
        },
    }


def validate_scope(historical, remaining):
    if (
        set(historical) != SMOKE_IDS
        or len(remaining) != 46
        or set(historical) & set(remaining)
        or set(historical) | set(remaining) != {f"case-{i:03}" for i in range(1, 55)}
    ):
        raise ValueError("remaining_scope_invalid")


def sdk_budget(packets):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.core.llm_factory import SafeChatOpenAI
    from app.evaluation.single_pass_packet import OUTPUT_ALLOWANCE, SYSTEM_PROMPT

    model = SafeChatOpenAI(
        model="qwen-max",
        api_key="offline-placeholder",
        temperature=0,
        streaming=False,
        max_tokens=384,
    )
    maximum = 0
    try:
        for p in packets.values():
            body = model._get_request_payload(
                [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=p.text)],
                tools=[convert_to_openai_tool(SinglePassChoice)],
                tool_choice={"type": "function", "function": {"name": "SinglePassChoice"}},
                parallel_tool_calls=False,
            )
            size = len(canonical(body).encode()) + OUTPUT_ALLOWANCE
            if size > 16384:
                raise ValueError("sdk_transport_budget_exceeded")
            maximum = max(maximum, size)
    finally:
        model.root_client.close()
        asyncio.run(model.root_async_client.close())
    return maximum


def parent_state(packets, config):
    with closing(sqlite3.connect("file:" + DB.resolve().as_posix() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM attempts WHERE experiment=? ORDER BY case_id",
                ("v51-r2-all54-20261104",),
            )
        ]
    if {r["case_id"] for r in rows} != SMOKE_IDS or len(rows) != 8:
        raise ValueError("historical_scope_changed")
    for r in rows:
        if r["fingerprint"] != configuration_fingerprint(packets[r["case_id"]], config):
            raise ValueError("historical_fingerprint_changed")
        if r["case_id"] == "case-026":
            if r["state"] != "reserved" or r["result"] is not None:
                raise ValueError("historical_unknown_changed")
        else:
            result = json.loads(r["result"])
            if (
                not protocol_gate([{**result, "case_id": r["case_id"]}], {r["case_id"]})
                or r["state"] != "complete"
            ):
                raise ValueError("historical_result_changed")
    return digest(rows)


def prepare():
    if source_snapshot() != NEW_SOURCE:
        raise ValueError("reviewed_source_changed")
    parent = json.loads((PARENT / "experiment.json").read_text("utf-8"))
    archive = PARENT / "source-frozen-1d3bad32.zip"
    if (
        hashlib.sha256(archive.read_bytes()).hexdigest()
        != "0368bbb9292334359785e3426972635d68866026a35671bba41b79dde6c2f12b"
    ):
        raise ValueError("frozen_archive_changed")
    with zipfile.ZipFile(archive) as z:
        before = {n: z.read(n) for n in z.namelist()}
    entries = [
        [str(PurePosixPath(n).relative_to("app")), hashlib.sha256(b).hexdigest()]
        for n, b in before.items()
    ]
    if "sha256:" + digest(entries) != OLD_SOURCE:
        raise ValueError("archive_source_mismatch")
    after = {p.as_posix(): p.read_bytes() for p in Path("app").rglob("*.py")}
    attestation = attest_algorithms(before, after)
    context = parent["config"]["preflight_context"]
    if parent["config"]["code_revision"] != OLD_SOURCE or context["source_sha256"] != OLD_SOURCE:
        raise ValueError("old_source_mismatch")
    # Rehash current inputs; do not parse new evaluation labels.
    if (
        input_snapshot(ALL_ROOT, ALL_ANSWERS) != context["input_sha256"]
        or input_snapshot(DEVELOPMENT_ROOT, DEVELOPMENT_ANSWERS)
        != context["development_input_sha256"]
    ):
        raise ValueError("raw_input_changed")
    package = load_package(PARENT / "preflight.json", context)  # Original context stays unchanged.
    if (
        not package["telemetry_validation"]["passed"]
        or package["telemetry_validation"]["case_count"] != 54
    ):
        raise ValueError("old_preflight_incomplete")
    packets = cached_packets(package["telemetry_validation"], set(parent["case_ids"]))
    attestation.update(
        original_context=context,
        original_preflight_sha256=hashlib.sha256(
            (PARENT / "preflight.json").read_bytes()
        ).hexdigest(),
        historical_summary_sha256=hashlib.sha256(
            (PARENT / "summary.json").read_bytes()
        ).hexdigest(),
        sdk_max_transport_bytes=sdk_budget(packets),
        historical_rows_sha256=parent_state(packets, parent["config"]),
        scope="Explicit immutable packet reuse, not newly re-extracted telemetry",
    )
    remaining = set(packets) - SMOKE_IDS
    validate_scope(SMOKE_IDS, remaining)
    config = {
        **parent["config"],
        "code_revision": NEW_SOURCE,
        "smoke_case_ids": sorted(GATE),
        "input_reuse_attestation_sha256": digest(attestation),
        "parent_experiment": "v51-r2-all54-20261104",
        "executor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    manifest = {
        "experiment_id": NEW_ID,
        "config": config,
        "case_ids": sorted(remaining),
        "data_dir": str(ALL_ROOT.resolve()),
        "answers_file": str(ALL_ANSWERS.resolve()),
        "output_dir": str(OUTPUT.resolve()),
        "historical_smoke_gate_passed": False,
        "continuation_context": "2026-11-07 user resumed after requested persistence repair; historical8 excluded",
        "gate_case_ids": sorted(GATE),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "experiment.json"
    if path.exists():
        if json.loads(path.read_text("utf-8")) != manifest:
            raise ValueError("configuration_mismatch")
    elif any(OUTPUT.iterdir()):
        raise ValueError("output_directory_not_empty")
    else:
        _write(path, manifest)
        _write(OUTPUT / "input-reuse-attestation.json", attestation)
    if json.loads((OUTPUT / "input-reuse-attestation.json").read_text("utf-8")) != attestation:
        raise ValueError("attestation_changed")
    # Test the repaired publication path on the same filesystem before paid work.
    publish_once(
        OUTPUT / "storage-receipt-probe.json",
        canonical({"source": NEW_SOURCE, "executor": config["executor_sha256"]}).encode(),
    )
    _write(OUTPUT / "storage-probe.json", {"passed": True, "source": NEW_SOURCE})
    return parent, packets, remaining, config, manifest


def aggregate(parent, new_runs, remaining):
    proof = json.loads((OUTPUT / "input-reuse-attestation.json").read_text("utf-8"))
    old = verified_parent_summary(PARENT / "summary.json", proof["historical_summary_sha256"])[
        "runs"
    ]
    if len(old) != 8 or {r["case_id"] for r in old} != SMOKE_IDS:
        raise ValueError("historical_summary_changed")
    old = copy.deepcopy(old)
    for r in old:
        r.update(
            experiment_id=parent["experiment_id"],
            execution_source_sha256=OLD_SOURCE,
            cohort="historical8",
        )
    current = copy.deepcopy(new_runs)
    for r in current:
        r.update(
            experiment_id=NEW_ID,
            execution_source_sha256=NEW_SOURCE,
            cohort="after_persistence_repair",
        )
    combined = old + current
    if (
        len({r["case_id"] for r in combined}) != len(combined)
        or not {r["case_id"] for r in current} <= remaining
    ):
        raise ValueError("combined_scope_overlap")
    result = summarize_runs(combined)
    result.update(
        expected_case_count=54,
        complete=len(combined) == 54,
        evaluation_set="historical8_plus_repaired46",
        revision=REVISION,
        historical_smoke_gate_passed=False,
        source_cohorts={"historical8": OLD_SOURCE, "repaired46": NEW_SOURCE},
        limitation="Mixed November4/November7 execution-source cohorts using identical frozen diagnosis inputs and named model alias; not an independent blind or single-source experiment. case026 unknown remains a miss; total usage incomplete.",
    )
    _write(OUTPUT / "combined54-summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--stage", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.execute and args.stage == "full" and not args.resume:
        parser.error("Full requires explicit resume of passed repaired three-case gate")
    parent, packets, remaining, config, manifest = prepare()
    print(
        canonical(
            {
                "prepared": True,
                "source": NEW_SOURCE,
                "remaining": len(remaining),
                "new_gate": sorted(GATE),
                "historical8_not_resent": sorted(SMOKE_IDS),
                "max_bytes": 16384,
                "execute": args.execute,
            }
        ),
        flush=True,
    )
    if not args.execute:
        return
    journal = AttemptJournal(DB)
    journal.bind_experiment(NEW_ID, digest(manifest))
    if args.stage == "full":
        prior = []
        for cid in GATE:
            row = journal.load(NEW_ID, cid)
            if row and row["state"] == "reserved":
                row = journal.recover(NEW_ID, cid, configuration_fingerprint(packets[cid], config))
            if row and row["state"] == "complete":
                prior.append(journal.cached(row))
        if not protocol_gate(prior, GATE):
            raise ValueError("repaired_smoke_gate_not_passed")
    from app.core.llm_factory import llm_factory

    summary = asyncio.run(
        evaluate_cases(
            Pilot(ALL_ROOT, answers_file=ALL_ANSWERS),
            output_dir=OUTPUT,
            journal=journal,
            experiment_id=NEW_ID,
            config=config,
            prepared_packets=packets,
            model_factory=lambda _: llm_factory.create_chat_model(
                model="qwen-max", temperature=0, streaming=False
            ).model_copy(update={"max_tokens": 384}),
            telemetry_profile=config["telemetry_profile"],
            timeout_seconds=config["timeout_seconds"],
            resume=args.resume,
            case_ids=GATE if args.stage == "smoke" else remaining,
        )
    )
    summary.update(
        expected_case_count=46, complete=summary["case_count"] == 46, evaluation_set="remaining46"
    )
    _write(OUTPUT / "summary.json", summary)
    if (
        parent_state(packets, parent["config"])
        != json.loads((OUTPUT / "input-reuse-attestation.json").read_text("utf-8"))[
            "historical_rows_sha256"
        ]
    ):
        raise ValueError("historical_rows_modified")
    combined = aggregate(parent, summary["runs"], remaining)
    print(canonical({k: v for k, v in combined.items() if k not in {"runs", "strata"}}), flush=True)


if __name__ == "__main__":
    main()
