from datetime import UTC, datetime, timedelta

import pytest

from app.repositories.remediation import InMemoryRemediationRepository

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def validated_snapshot(now):
    return [
        {
            "evidence_type": kind, "source": "trusted-observer", "success": True,
            "validation_reason": "valid", "target_service": "orders",
            "target_instance": "orders-1", "collected_at": now.isoformat(),
            "validated_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=10)).isoformat(),
        }
        for kind in ("alert", "metric")
    ]


def action_values(**overrides):
    values = {
        "id": "action-1",
        "diagnosis_id": "diagnosis-1",
        "idempotency_key": "diagnosis-1:restart_instance:orders:orders-1",
        "action_type": "restart_instance",
        "target_service": "orders",
        "risk_level": "r1",
        "status": "proposed",
        "intent_payload": {
            "action_type": "restart_instance",
            "service_name": "orders",
            "instance_id": "orders-1",
            "reason": "unhealthy",
        },
        "evidence_snapshot": validated_snapshot(NOW),
        "policy_decision": "allow",
        "policy_reason": "policy_allowed",
        "requested_by": "agent",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_create_is_idempotent_by_idempotency_key():
    repository = InMemoryRemediationRepository()

    first, first_created = await repository.create(action_values(), enqueue=False)
    duplicate, duplicate_created = await repository.create(
        action_values(id="action-2"), enqueue=False
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"] == "action-1"


@pytest.mark.asyncio
async def test_list_filters_by_status_without_unbounded_payloads():
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(), enqueue=False)
    await repository.create(
        action_values(
            id="action-2",
            idempotency_key="diagnosis-1:scale_out:orders",
            action_type="scale_out",
            status="denied",
        ),
        enqueue=False,
    )

    proposed = await repository.list(status="proposed", limit=1)

    assert [item["id"] for item in proposed] == ["action-1"]
    with pytest.raises(ValueError, match="limit"):
        await repository.list(limit=101)


@pytest.mark.asyncio
async def test_compare_and_set_rejects_illegal_state_transition():
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(status="awaiting_approval"), enqueue=False)

    changed = await repository.compare_and_set("action-1", {"executing"}, status="succeeded")

    assert changed is False
    assert (await repository.get("action-1"))["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_claim_execution_only_succeeds_once(monkeypatch):
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW)
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(status="approved"), enqueue=False)

    first = await repository.claim_execution("action-1", "worker-1")
    duplicate = await repository.claim_execution("action-1", "worker-2")

    assert first == "execute"
    assert duplicate == "busy"
    action = await repository.get("action-1")
    assert action["status"] == "executing"
    assert action["worker_name"] == "worker-1"


@pytest.mark.asyncio
async def test_create_eligible_action_commits_outbox_atomically():
    repository = InMemoryRemediationRepository()

    action, created = await repository.create(action_values(), enqueue=True)

    assert created is True
    assert repository.outbox_events == [
        {
            "diagnosis_id": "diagnosis-1",
            "event_type": "remediation.execute",
            "task_name": "app.tasks.remediation.run_remediation",
            "queue_name": "remediation.control",
            "payload": {"args": [action["id"]], "task_id": action["id"]},
            "status": "pending",
        }
    ]


@pytest.mark.asyncio
async def test_decide_approval_enqueues_once_and_expiry_wins_when_due():
    repository = InMemoryRemediationRepository()
    await repository.create(
        action_values(
            status="awaiting_approval",
            approval_expires_at=NOW + timedelta(minutes=15),
        ),
        enqueue=False,
    )

    approved = await repository.decide("action-1", "approve", "operator-1", NOW)
    duplicate = await repository.decide("action-1", "approve", "operator-1", NOW)

    assert approved == "approved"
    assert duplicate == "conflict"
    assert len(repository.outbox_events) == 1

    expired_repository = InMemoryRemediationRepository()
    await expired_repository.create(
        action_values(
            status="awaiting_approval",
            approval_expires_at=NOW,
        ),
        enqueue=False,
    )
    assert await expired_repository.decide("action-1", "approve", "operator-1", NOW) == "expired"
    assert expired_repository.outbox_events == []


@pytest.mark.asyncio
async def test_stale_prepared_execution_is_claimed_for_reconciliation(monkeypatch):
    repository = InMemoryRemediationRepository()
    prepared = {
        "intent": action_values()["intent_payload"],
        "pre_change_snapshot": {"known": True},
        "verification_spec": {"kind": "generic"},
        "rollback_token": "opaque-token",
    }
    await repository.create(
        action_values(
            status="executing",
            prepared_payload=prepared,
            updated_at=NOW - timedelta(minutes=10),
        ),
        enqueue=False,
    )
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW)

    claim = await repository.claim_execution("action-1", "recovery-worker", lease_seconds=300)

    assert claim == "reconcile"
    assert (await repository.get("action-1"))["status"] == "reconciling"


@pytest.mark.asyncio
async def test_expire_due_approvals_releases_active_capacity():
    repository = InMemoryRemediationRepository()
    await repository.create(
        action_values(
            status="awaiting_approval",
            approval_expires_at=NOW - timedelta(seconds=1),
        ),
        enqueue=False,
    )

    expired = await repository.expire_due_approvals(NOW)

    assert expired == 1
    assert await repository.count_active("orders") == 0
    assert (await repository.get("action-1"))["status"] == "expired"


@pytest.mark.asyncio
async def test_swept_expiry_returns_expired_for_later_decisions():
    repository = InMemoryRemediationRepository()
    await repository.create(
        action_values(status="awaiting_approval", approval_expires_at=NOW), enqueue=False
    )
    await repository.expire_due_approvals(NOW)

    assert await repository.decide("action-1", "approve", "operator", NOW) == "expired"
    assert await repository.decide("action-1", "reject", "operator", NOW) == "expired"
    assert repository.outbox_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "approved"])
async def test_claim_denies_legacy_evidence_without_validation_metadata(monkeypatch, status):
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW)
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(status=status, evidence_snapshot=[]), enqueue=True)

    claim = await repository.claim_execution("action-1", "worker")

    assert claim == "ignored"
    action = await repository.get("action-1")
    assert action["status"] == "denied"
    assert action["completed_at"] == NOW
    assert action.get("started_at") is None
    assert action.get("worker_name") is None
    assert action["error"]
    assert await repository.count_active() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "approved"])
@pytest.mark.parametrize("invalidity", ["expired", "wrong_service", "wrong_instance", "missing_metadata", "future", "malformed"])
async def test_claim_rejects_expired_or_unbound_frozen_evidence(monkeypatch, status, invalidity):
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: NOW)
    snapshot = validated_snapshot(NOW)
    for item in snapshot:
        if invalidity == "expired":
            item["valid_until"] = NOW.isoformat()
        elif invalidity == "wrong_service":
            item["target_service"] = "payments"
        elif invalidity == "wrong_instance":
            item["target_instance"] = "orders-2"
        elif invalidity == "missing_metadata":
            item.pop("validation_reason")
        elif invalidity == "future":
            item["collected_at"] = (NOW + timedelta(days=1)).isoformat()
        elif invalidity == "malformed":
            item["valid_until"] = "not-a-timestamp"
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(status=status, evidence_snapshot=snapshot), enqueue=True)

    claim = await repository.claim_execution("action-1", "worker")

    assert claim == "ignored"
    action = await repository.get("action-1")
    assert action["status"] == "denied"
    assert action["error"].startswith("frozen_evidence_invalid_or_expired")
    assert action["completed_at"] == NOW
    assert action.get("started_at") is None
    assert await repository.count_active("orders") == 0


@pytest.mark.asyncio
async def test_approval_rechecks_frozen_evidence_atomically():
    repository = InMemoryRemediationRepository()
    await repository.create(action_values(status="awaiting_approval", evidence_snapshot=[],
        approval_expires_at=NOW + timedelta(minutes=10)), enqueue=False)
    assert await repository.decide("action-1", "approve", "operator", NOW) == "expired"
    assert repository.outbox_events == []
    assert (await repository.get("action-1"))["status"] == "expired"
