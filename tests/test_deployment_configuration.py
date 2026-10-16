"""Exercise effective Compose configuration, not source-string matching."""

import json
import os
import subprocess


def test_diagnosis_policy_and_custom_queue_names_reach_consumers():
    environment = {
        **os.environ,
        "REMEDIATION_MODE": "assisted",
        "REMEDIATION_GLOBAL_ACTIVE_LIMIT": "3",
        "REMEDIATION_EVIDENCE_MAX_AGE_SECONDS": "120",
        "REMEDIATION_EVIDENCE_CLOCK_SKEW_SECONDS": "5",
        "CELERY_DEFAULT_QUEUE": "test.default",
        "CELERY_CRITICAL_QUEUE": "test.critical",
        "DASHSCOPE_API_KEY": "test-key",
        "TENCENTCLOUD_SECRET_ID": "test-id",
        "TENCENTCLOUD_SECRET_KEY": "test-secret",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", "vector-database.yml", "config", "--format", "json"],
        env=environment, capture_output=True, text=True, check=True,
    )
    services = json.loads(result.stdout)["services"]
    for name, queue in [("diagnosis-default-worker", "test.default"), ("diagnosis-critical-worker", "test.critical")]:
        worker = services[name]
        assert worker["environment"].get("REMEDIATION_MODE") == "assisted"
        assert worker["environment"].get("REMEDIATION_GLOBAL_ACTIVE_LIMIT") == "3"
        assert "--queues=" + queue in worker["command"]
    assert "DASHSCOPE_API_KEY" not in services["remediation-worker"]["environment"]

    for name in ("diagnosis-default-worker", "diagnosis-critical-worker", "remediation-worker"):
        assert services[name]["environment"]["REMEDIATION_EVIDENCE_MAX_AGE_SECONDS"] == "120"
        assert services[name]["environment"]["REMEDIATION_EVIDENCE_CLOCK_SKEW_SECONDS"] == "5"
