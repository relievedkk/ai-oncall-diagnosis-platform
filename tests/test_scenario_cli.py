import json
import subprocess
import sys


def test_comparison_cli_saves_each_actual_run_and_summary(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.evaluation.compare_cli",
            "--case",
            "cpu_load",
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["sample_count"] == 2
    runs = [path for path in tmp_path.glob("*.json") if path.name != "summary.json"]
    assert len(runs) == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["strategy"] for path in runs} == {
        "agent",
        "baseline",
    }


def test_cli_live_requires_explicit_opt_in(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.evaluation.compare_cli",
            "--mode",
            "live",
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert completed.returncode == 2
    assert "--allow-live" in completed.stderr
    assert list(tmp_path.iterdir()) == []
