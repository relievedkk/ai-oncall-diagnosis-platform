from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import config
from app.core.auth import validate_security_configuration
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.repositories.remediation import InMemoryRemediationRepository
from app.services.queue_observer import QueueObserver


def _valid_base_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "host", "127.0.0.1")
    monkeypatch.setattr(config, "auth_enabled", False)
    monkeypatch.setattr(config, "web_login_enabled", False)
    monkeypatch.setattr(config, "alertmanager_webhook_token", "b" * 64)


def test_unknown_remediation_adapter_fails_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _valid_base_configuration(monkeypatch)
    monkeypatch.setattr(config, "remediation_adapter", "kubernetes")

    with pytest.raises(RuntimeError, match="REMEDIATION_ADAPTER"):
        validate_security_configuration()


def test_dry_run_adapter_is_the_only_supported_runtime_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _valid_base_configuration(monkeypatch)
    monkeypatch.setattr(config, "remediation_adapter", "dry_run")

    validate_security_configuration()


def test_remediation_alert_rules_cover_failure_rollback_and_circuit_breaker() -> None:
    rules = Path("monitoring/alerts.yml").read_text(encoding="utf-8")

    assert "RemediationExecutionFailed" in rules
    assert 'status="failed"' in rules
    assert "RemediationRollbackCircuitBreaker" in rules
    assert 'status=~"rolling_back|rolled_back"' in rules
    assert "RemediationApprovalStale" in rules
    assert "oncall_remediation_oldest_awaiting_approval_seconds" in rules
    assert "RemediationQueueHasNoConsumer" in rules
    assert 'queue="remediation.control"' in rules
    assert 'status="proposed"' not in rules


def test_ci_runs_targeted_harness_lint_and_monitoring_compose_validation() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "ruff check app/harness app/repositories/remediation.py" in workflow
    assert "tests/test_remediation_*.py" in workflow
    assert "docker compose -f monitoring.yml config --quiet" in workflow


async def test_queue_observer_exports_stale_approval_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    repository = InMemoryRemediationRepository()
    await repository.create(
        {
            "id": "action-stale",
            "diagnosis_id": "diagnosis-stale",
            "idempotency_key": "diagnosis-stale:intent",
            "action_type": "restart_instance",
            "target_service": "orders",
            "risk_level": "r2",
            "status": "awaiting_approval",
            "intent_payload": {},
            "evidence_snapshot": [],
            "policy_decision": "require_approval",
            "policy_reason": "approval_required",
            "requested_by": "agent",
            "created_at": now - timedelta(minutes=20),
            "updated_at": now - timedelta(minutes=20),
        },
        enqueue=False,
    )
    observer = QueueObserver(InMemoryDiagnosisRepository(), repository)

    async def fake_queues():
        return {}

    monkeypatch.setattr(observer, "rabbitmq_queues", fake_queues)
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: now)

    snapshot = await observer.collect_once()

    assert snapshot["remediation"]["action_counts"] == {"awaiting_approval": 1}
    assert snapshot["remediation"]["oldest_awaiting_approval_seconds"] == 1200
