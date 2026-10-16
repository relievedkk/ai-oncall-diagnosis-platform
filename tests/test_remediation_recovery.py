"""Crash recovery must finish every durable phase without replaying side effects."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.harness.contracts import GatewayEvent, RestartInstanceIntent
from app.harness.gateway import DryRunRemediationAdapter, RemediationGateway
from app.repositories.remediation import InMemoryRemediationRepository
from app.services.remediation_service import RemediationConflict, RemediationService
from app.tasks.remediation import execute_remediation

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


class RecoveryAdapter(DryRunRemediationAdapter):
    def __init__(self):
        self.executions = 0
        self.rollbacks = 0

    async def execute(self, prepared):
        self.executions += 1
        return await super().execute(prepared)

    async def rollback(self, prepared):
        self.rollbacks += 1
        return await super().rollback(prepared)


async def setup_action(monkeypatch, status, *, prepared=True):
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW)
    adapter = RecoveryAdapter()
    intent = RestartInstanceIntent(
        action_type="restart_instance",
        service_name="orders",
        instance_id="orders-1",
        reason="unhealthy",
    )
    payload = (await adapter.prepare(intent)).model_dump(mode="json") if prepared else None
    repository = InMemoryRemediationRepository()
    await repository.create(
        {
            "id": "action-1",
            "diagnosis_id": "diagnosis-1",
            "idempotency_key": "recovery-1",
            "action_type": "restart_instance",
            "target_service": "orders",
            "status": status,
            "intent_payload": intent.model_dump(mode="json"),
            "prepared_payload": payload,
            "evidence_snapshot": [
                {
                    "evidence_type": kind, "source": "trusted-observer", "success": True,
                    "validation_reason": "valid", "target_service": "orders",
                    "target_instance": "orders-1", "collected_at": NOW.isoformat(),
                    "validated_at": NOW.isoformat(),
                    "valid_until": (NOW + timedelta(minutes=10)).isoformat(),
                }
                for kind in ("alert", "metric")
            ],
            "updated_at": NOW - timedelta(minutes=10),
        },
        enqueue=False,
    )
    service = RemediationService(repository=repository, now_factory=lambda: NOW)
    return repository, service, adapter


@pytest.mark.asyncio
async def test_stale_verification_finishes_without_reexecuting(monkeypatch):
    repository, service, adapter = await setup_action(monkeypatch, "verifying")

    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "succeeded"
    assert await repository.count_active() == 0
    assert adapter.executions == adapter.rollbacks == 0


@pytest.mark.asyncio
async def test_stale_rollback_fails_closed_without_repeating_rollback(monkeypatch):
    repository, service, adapter = await setup_action(monkeypatch, "rolling_back")

    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "failed"
    action = await repository.get("action-1")
    assert action["completed_at"] == NOW
    assert action["circuit_breaker_open"] is True
    assert "manual" in action["error"]
    assert await repository.count_active() == 0
    assert adapter.executions == adapter.rollbacks == 0


@pytest.mark.asyncio
async def test_stale_verification_missing_prepared_payload_fails_closed(monkeypatch):
    repository, service, adapter = await setup_action(monkeypatch, "verifying", prepared=False)

    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "failed"
    assert await repository.count_active() == 0
    assert (await repository.get("action-1"))["circuit_breaker_open"] is True
    assert adapter.executions == 0


@pytest.mark.asyncio
async def test_old_claim_cannot_apply_event_after_recovery_takes_ownership(monkeypatch):
    repository, service, _adapter = await setup_action(monkeypatch, "executing")
    await repository.claim_execution("action-1", "old-claim")
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW + timedelta(minutes=10))
    await repository.claim_execution("action-1", "new-claim")

    with pytest.raises(RemediationConflict):
        await service.apply_gateway_event(
            "action-1", GatewayEvent(status="verifying"), claim_token="old-claim"
        )
    assert (await repository.get("action-1"))["status"] == "reconciling"


@pytest.mark.asyncio
async def test_fencing_is_atomic_when_claim_changes_after_service_reads_action(monkeypatch):
    repository, service, _adapter = await setup_action(monkeypatch, "executing")
    await repository.claim_execution("action-1", "old-claim")
    original_get = repository.get

    async def takeover_after_read(action_id):
        snapshot = await original_get(action_id)
        monkeypatch.setattr(
            "app.repositories.remediation.utc_now", lambda: NOW + timedelta(minutes=10)
        )
        await repository.claim_execution(action_id, "new-claim")
        repository.get = original_get
        return snapshot

    repository.get = takeover_after_read
    with pytest.raises(RemediationConflict):
        await service.apply_gateway_event(
            "action-1", GatewayEvent(status="verifying"), claim_token="old-claim"
        )
    assert (await repository.get("action-1"))["status"] == "reconciling"


@pytest.mark.asyncio
async def test_corrupt_prepared_payload_does_not_retry_forever(monkeypatch):
    repository, service, adapter = await setup_action(monkeypatch, "verifying")
    await repository.compare_and_set(
        "action-1",
        {"verifying"},
        prepared_payload={"corrupt": True},
        updated_at=NOW - timedelta(minutes=10),
    )

    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "failed"
    assert await repository.count_active() == 0
    assert (await repository.get("action-1"))["circuit_breaker_open"] is True
    assert adapter.executions == adapter.rollbacks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["executing", "reconciling", "verifying", "rolling_back"])
async def test_live_intermediate_claim_is_not_stolen(monkeypatch, status):
    repository, service, adapter = await setup_action(monkeypatch, status)
    await repository.compare_and_set("action-1", {status}, updated_at=NOW)

    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "busy"
    assert (await repository.get("action-1"))["status"] == status
    assert adapter.executions == adapter.rollbacks == 0


@pytest.mark.asyncio
async def test_stale_verification_keeps_known_execution_result_and_rolls_back_failed_guards(
    monkeypatch,
):
    repository, service, adapter = await setup_action(monkeypatch, "verifying")
    await repository.compare_and_set(
        "action-1",
        {"verifying"},
        result_payload={"success": True, "details": {"applied": True}},
        updated_at=NOW - timedelta(minutes=10),
    )

    async def failed_observation(prepared):
        return {"known": True, "success": False, "dry_run": True}

    adapter.observe = failed_observation
    outcome = await execute_remediation(
        "action-1", "worker", service=service, gateway=RemediationGateway(adapter)
    )

    assert outcome == "rolled_back"
    assert adapter.executions == 0
    assert adapter.rollbacks == 1
    assert await repository.count_active() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["execution_timeout", "after_execution", "reconcile"])
async def test_unknown_execution_outcome_opens_durable_circuit(monkeypatch, path):
    repository, service, adapter = await setup_action(
        monkeypatch, "executing" if path == "reconcile" else "proposed"
    )

    async def unknown_observation(prepared):
        return {"known": False}

    async def uncertain_execution(prepared):
        adapter.executions += 1
        await asyncio.Event().wait()

    adapter.observe = unknown_observation
    if path == "execution_timeout":
        adapter.execute = uncertain_execution
    gateway = RemediationGateway(adapter, execution_timeout_seconds=0.001)

    outcome = await execute_remediation("action-1", "worker", service=service, gateway=gateway)

    assert outcome == "failed"
    action = await repository.get("action-1")
    assert action["status"] == "failed"
    assert action["error"] == "execution outcome remains unknown"
    assert action["completed_at"] == NOW
    assert action.get("circuit_breaker_open") is True
    assert action["circuit_opened_at"] == NOW
    assert await repository.is_circuit_open("orders") is True
    assert await repository.count_active() == 0
    assert adapter.executions == (0 if path == "reconcile" else 1)
    assert adapter.rollbacks == 0
