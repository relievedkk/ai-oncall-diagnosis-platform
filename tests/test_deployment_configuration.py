"""Exercise effective Compose configuration, not source-string matching."""

import json
import os
import subprocess

SAFE_INFRASTRUCTURE = {
    "POSTGRES_PASSWORD": "p" * 32,
    "RABBITMQ_DEFAULT_PASS": "r" * 32,
    "MINIO_ACCESS_KEY": "oncall-test-access",
    "MINIO_SECRET_KEY": "m" * 40,
}


def render_services(**overrides):
    environment = {**os.environ, **SAFE_INFRASTRUCTURE, **overrides}
    result = subprocess.run(
        ["docker", "compose", "-f", "vector-database.yml", "config", "--format", "json"],
        env=environment, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["services"]


def test_compose_refuses_missing_infrastructure_credentials(tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in SAFE_INFRASTRUCTURE
    }
    result = subprocess.run(
        [
            "docker", "compose", "--env-file", str(empty_env),
            "-f", "vector-database.yml", "config",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "required" in result.stderr.lower()


def test_milvus_receives_the_configured_minio_credentials():
    services = render_services()

    assert services["standalone"]["environment"]["MINIO_ACCESS_KEY_ID"] == SAFE_INFRASTRUCTURE["MINIO_ACCESS_KEY"]
    assert services["standalone"]["environment"]["MINIO_SECRET_ACCESS_KEY"] == SAFE_INFRASTRUCTURE["MINIO_SECRET_KEY"]


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
    services = render_services(**environment)
    for name, queue in [("diagnosis-default-worker", "test.default"), ("diagnosis-critical-worker", "test.critical")]:
        worker = services[name]
        assert worker["environment"].get("REMEDIATION_MODE") == "assisted"
        assert worker["environment"].get("REMEDIATION_GLOBAL_ACTIVE_LIMIT") == "3"
        assert "--queues=" + queue in worker["command"]
    assert "DASHSCOPE_API_KEY" not in services["remediation-worker"]["environment"]

    for name in ("diagnosis-default-worker", "diagnosis-critical-worker", "remediation-worker"):
        assert services[name]["environment"]["REMEDIATION_EVIDENCE_MAX_AGE_SECONDS"] == "120"
        assert services[name]["environment"]["REMEDIATION_EVIDENCE_CLOCK_SKEW_SECONDS"] == "5"


def test_workers_use_release_catalog_without_windows_config_bind_mount():
    services = render_services(ONCALL_CONFIG_ROOT=r"D:\unshared-oncall-config")

    for name in (
        "diagnosis-default-worker",
        "diagnosis-critical-worker",
        "remediation-worker",
        "outbox-publisher",
    ):
        worker = services[name]
        assert worker["environment"]["SERVICE_CATALOG_PATH"] == "/app/config/service_catalog.json"
        assert all(volume["target"] != "/app-config/config" for volume in worker.get("volumes", []))
