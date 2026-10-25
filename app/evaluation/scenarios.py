"""Versioned synthetic inputs; private expectations are deliberately not loaded here."""

import json
from pathlib import Path

SCENARIO_DIRECTORY = Path(__file__).resolve().parents[2] / "evals" / "scenarios" / "v1"


def load_cases() -> list[dict]:
    """Return fresh input objects, never labels, scripts or human scores."""
    return json.loads((SCENARIO_DIRECTORY / "inputs.json").read_text(encoding="utf-8"))


def load_case(case_id: str) -> dict:
    """Resolve an allowlisted identifier without interpreting it as a path."""
    for case in load_cases():
        if case["case_id"] == case_id:
            return case
    raise ValueError(f"Unknown case: {case_id!r}")
