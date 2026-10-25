"""Synthetic comparison CLI; real model requests require an explicit opt-in."""

import argparse
import asyncio
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare Agent and fixed metrics/log baseline on synthetic fixtures"
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Built-in case ID; repeat to select several",
    )
    parser.add_argument("--mode", choices=("replay", "live"), default="replay")
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Explicitly authorize configured real model calls on synthetic tools",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/scenario-comparison"))
    args = parser.parse_args(argv)
    if args.mode == "live" and not args.allow_live:
        parser.error("live mode requires --allow-live; it may consume model quota")
    from app.evaluation.comparison import _compare

    try:
        summary = asyncio.run(
            _compare(
                args.case_ids,
                mode=args.mode,
                repeats=args.repeats,
                max_calls=args.max_calls,
                timeout_seconds=args.timeout_seconds,
                output_directory=args.output_dir,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("Cancelled. Completed per-run artifacts remain in the output directory.")
        return 130
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "runs"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
