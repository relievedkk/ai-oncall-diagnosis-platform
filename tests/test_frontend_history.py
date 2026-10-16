"""Run behavioral frontend regressions with Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_history_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for frontend behavior tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("frontend_history.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
