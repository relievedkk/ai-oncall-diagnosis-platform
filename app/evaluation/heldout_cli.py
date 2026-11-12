"""Grouped holdout commands. Validation has no provider configuration or label dependency."""

import argparse
import asyncio
from pathlib import Path

from app.core.single_pass import canonical
from app.evaluation.heldout_dataset import DATA_FILES, digest, file_sha, safe_child
from app.evaluation.single_pass_runner import source_snapshot
from app.evaluation.single_pass_storage import atomic_json_write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--index", required=True, type=Path)
    prepare.add_argument("--history-answers", required=True, action="append", type=Path)
    prepare.add_argument("--history-root", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    for name in ("inventory", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--tasks", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    live = commands.add_parser("live")
    live.add_argument("--run", required=True, type=Path)
    live.add_argument("--allow-live", action="store_true")
    live.add_argument("--reviewed-run-sha256", required=True)
    live.add_argument("--phase", choices=("smoke", "full"), default="smoke")
    live.add_argument("--resume", action="store_true")
    score = commands.add_parser("score")
    score.add_argument("--run", required=True, type=Path)
    score.add_argument("--answers", required=True, type=Path)
    score.add_argument("--offline-only", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        from app.evaluation.heldout_prepare import prepare_grouped

        result = prepare_grouped(
            args.index,
            args.history_answers,
            args.history_root,
            args.output,
            progress=lambda n, total: print(f"Preparing {n}/{total}", flush=True),
        )
    elif args.command == "inventory":
        from app.evaluation.evidence_inventory import audit_inventory
        from app.evaluation.rcaeval import Pilot

        if args.output.exists():
            parser.error("output_already_exists")
        pilot = Pilot(args.tasks)
        paths = {
            f"{cid}/{name}": safe_child(pilot.root, f"{source}/{name}")
            for cid, source in pilot.ids.items()
            for name in DATA_FILES
            if (pilot.root / source / name).exists()
        }
        inputs = {name: file_sha(path) for name, path in paths.items()}
        code = source_snapshot()
        result = audit_inventory(pilot, progress=lambda cid: print(cid, flush=True))
        if (
            source_snapshot() != code
            or {name: file_sha(path) for name, path in paths.items()} != inputs
        ):
            raise ValueError("inventory_inputs_changed")
        result.update(
            source_sha256=code,
            input_files=inputs,
            input_sha256=digest(inputs),
            manifest_sha256=file_sha(args.tasks / "manifest.json"),
        )
        atomic_json_write(args.output, result)
    elif args.command == "validate":
        from app.evaluation.heldout_protocol import prepare_run

        result = prepare_run(args.tasks, args.output, progress=lambda cid: print(cid, flush=True))
    elif args.command == "score":
        from app.evaluation.heldout_score import score_run

        result = score_run(args.run, args.answers, offline_only=args.offline_only)
    else:
        if not args.allow_live:
            parser.error("explicit_live_required")
        from app.core.llm_factory import llm_factory
        from app.evaluation.heldout_live import run_live

        result = asyncio.run(
            run_live(
                args.run,
                allow_live=True,
                reviewed_sha=args.reviewed_run_sha256,
                phase=args.phase,
                resume=args.resume,
                model_config={
                    "model": "qwen-max",
                    "temperature": 0,
                    "max_tokens": 384,
                    "base_url": llm_factory.DASHSCOPE_BASE_URL,
                },
                model_factory=lambda _: llm_factory.create_chat_model(
                    model="qwen-max", temperature=0, streaming=False
                ).model_copy(update={"max_tokens": 384}),
            )
        )
    print(
        canonical(
            {
                k: v
                for k, v in result.items()
                if k not in {"cases", "artifacts", "statuses", "case_ids"}
            }
        ),
        flush=True,
    )
    return 0 if result.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
