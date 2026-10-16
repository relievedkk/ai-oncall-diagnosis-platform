"""Opt-in integration check against the built image and actual Compose bind mount."""

import os
import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("ONCALL_DOCKER_TESTS") != "1",
    reason="Set ONCALL_DOCKER_TESTS=1 with Docker ready",
)
def test_non_root_worker_can_write_local_and_shared_log_directories(tmp_path):
    marker = "native-mount-probe-" + uuid.uuid4().hex
    probe = Path("logs/cls-events") / ("." + marker)
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(marker, encoding="utf-8")
    compose_files = ["-f", "vector-database.yml"]
    if image := os.environ.get("ONCALL_DOCKER_IMAGE"):
        import json

        override = tmp_path / "worker-test-image.json"
        override.write_text(json.dumps({"services": {"diagnosis-default-worker": {"image": image}}}), encoding="utf-8")
        compose_files += ["-f", str(override)]
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                *compose_files,
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "sh",
                "diagnosis-default-worker",
                "-c",
                'test "$(id -u)" = "10001" && test -w /app/logs && test -w /app/logs/cls-events'
                + f" && cat /app/logs/cls-events/{probe.name}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    finally:
        probe.unlink(missing_ok=True)
    assert result.returncode == 0, "Non-root logging directories are not writable: " + result.stderr
    assert result.stdout.strip() == marker, "Worker mount is not the native uploader's directory"
