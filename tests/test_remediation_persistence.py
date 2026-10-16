import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import DiagnosisJob, OutboxEvent, RemediationAction
from app.harness.contracts import GatewayEvent
from app.repositories.remediation import SQLAlchemyRemediationRepository
from app.services.remediation_service import RemediationConflict, RemediationService

pytestmark = pytest.mark.integration


@pytest.fixture
async def postgres_repository():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        if os.getenv("CI", "").casefold() == "true":
            pytest.fail("CI requires TEST_DATABASE_URL for PostgreSQL integration tests")
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if os.getenv("CI", "").casefold() == "true":
            pytest.fail(f"required CI PostgreSQL is unavailable: {exc}")
        pytest.skip(f"configured PostgreSQL is unavailable: {exc}")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyRemediationRepository(sessions)
    diagnosis_id = f"test-remediation-{uuid4()}"
    async with sessions.begin() as session:
        session.add(
            DiagnosisJob(
                diagnosis_id=diagnosis_id,
                event_key=f"event-{diagnosis_id}",
                trigger="test",
                status="completed",
                alert_status="firing",
                service="orders",
                alert_payload={},
            )
        )
    try:
        yield repository, sessions, diagnosis_id
    finally:
        async with sessions.begin() as session:
            await session.execute(
                delete(OutboxEvent).where(OutboxEvent.diagnosis_id == diagnosis_id)
            )
            await session.execute(
                delete(RemediationAction).where(RemediationAction.diagnosis_id == diagnosis_id)
            )
            await session.execute(
                delete(DiagnosisJob).where(DiagnosisJob.diagnosis_id == diagnosis_id)
            )
        await engine.dispose()


def values(diagnosis_id, **overrides):
    action_id = str(uuid4())
    now = datetime.now(UTC)
    payload = {
        "id": action_id,
        "diagnosis_id": diagnosis_id,
        "idempotency_key": f"{diagnosis_id}:{action_id}",
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
        "evidence_snapshot": [
            {
                "evidence_type": kind, "source": "trusted-observer", "success": True,
                "validation_reason": "valid", "target_service": "orders",
                "target_instance": "orders-1", "collected_at": now.isoformat(),
                "validated_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=10)).isoformat(),
            }
            for kind in ("alert", "metric")
        ],
        "policy_decision": "allow",
        "policy_reason": "policy_allowed",
        "requested_by": "agent",
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("token,status,lease_seconds", [
    ("replacement-owner", "running", 300),
    ("original-owner", "running", -1),
    ("original-owner", "completed", 300),
])
async def test_proposal_creation_rejects_invalid_diagnosis_owner_atomically(
    postgres_repository, token, status, lease_seconds
):
    from app.repositories.diagnosis import DiagnosisLeaseLostError

    repository, sessions, diagnosis_id = postgres_repository
    async with sessions.begin() as session:
        job = await session.get(DiagnosisJob, diagnosis_id, with_for_update=True)
        job.status = status
        job.execution_token = token
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    with pytest.raises(DiagnosisLeaseLostError):
        await repository.create(values(diagnosis_id), enqueue=True, execution_token="original-owner")
    assert await repository.count_for_diagnosis(diagnosis_id) == 0
    async with sessions() as session:
        assert await session.scalar(select(OutboxEvent).where(OutboxEvent.diagnosis_id == diagnosis_id)) is None
    async with sessions.begin() as session:
        job = await session.get(DiagnosisJob, diagnosis_id, with_for_update=True)
        job.status = "running"
        job.execution_token = "replacement-owner"
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    action, created = await repository.create(
        values(diagnosis_id), enqueue=True, execution_token="replacement-owner"
    )
    assert created and action["diagnosis_id"] == diagnosis_id


@pytest.mark.asyncio
async def test_postgres_unique_idempotency_and_atomic_outbox(postgres_repository):
    repository, sessions, diagnosis_id = postgres_repository
    first_values = values(diagnosis_id, idempotency_key=f"{diagnosis_id}:same")

    first, created = await repository.create(first_values, enqueue=True)
    duplicate, duplicate_created = await repository.create(
        values(diagnosis_id, idempotency_key=f"{diagnosis_id}:same"),
        enqueue=True,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]
    async with sessions() as session:
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.diagnosis_id == diagnosis_id)
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].payload == {"args": [first["id"]], "task_id": first["id"]}


@pytest.mark.asyncio
async def test_postgres_concurrent_decisions_have_one_outcome(postgres_repository):
    repository, _sessions, diagnosis_id = postgres_repository
    now = datetime.now(UTC)
    action, _created = await repository.create(
        values(
            diagnosis_id,
            status="awaiting_approval",
            approval_expires_at=now + timedelta(minutes=15),
        ),
        enqueue=False,
    )

    outcomes = await asyncio.gather(
        repository.decide(action["id"], "approve", "operator-a", now),
        repository.decide(action["id"], "reject", "operator-b", now),
    )

    assert sorted(outcomes) in (["approved", "conflict"], ["conflict", "rejected"])


@pytest.mark.asyncio
async def test_postgres_proposal_guard_serializes_across_repository_instances(
    postgres_repository,
):
    repository, sessions, _diagnosis_id = postgres_repository
    second_repository = SQLAlchemyRemediationRepository(sessions)
    active = 0
    maximum_active = 0

    async def guarded(repo):
        nonlocal active, maximum_active
        async with repo.proposal_guard():
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(guarded(repository), guarded(second_repository))

    assert maximum_active == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_execution_claim_has_one_owner(postgres_repository):
    repository, _sessions, diagnosis_id = postgres_repository
    action, _created = await repository.create(
        values(diagnosis_id, status="approved"), enqueue=False
    )

    claims = await asyncio.gather(
        repository.claim_execution(action["id"], "worker-a"),
        repository.claim_execution(action["id"], "worker-b"),
    )

    assert sorted(claims) == ["busy", "execute"]


@pytest.mark.asyncio
async def test_postgres_expiry_sweep_competes_safely_with_approval(
    postgres_repository,
):
    repository, sessions, diagnosis_id = postgres_repository
    now = datetime.now(UTC)
    action, _created = await repository.create(
        values(
            diagnosis_id,
            status="awaiting_approval",
            approval_expires_at=now,
        ),
        enqueue=False,
    )

    decision, expired_count = await asyncio.gather(
        repository.decide(action["id"], "approve", "operator", now),
        repository.expire_due_approvals(now),
    )

    assert decision in {"expired", "conflict"}
    assert expired_count in {0, 1}
    assert (await repository.get(action["id"]))["status"] == "expired"
    async with sessions() as session:
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.diagnosis_id == diagnosis_id)
                )
            ).all()
        )
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected_claim",
    [
        ("executing", "reconcile"),
        ("reconciling", "reconcile"),
        ("verifying", "reconcile"),
        ("rolling_back", "failed"),
    ],
)
async def test_postgres_recovers_every_expired_intermediate_phase(
    postgres_repository,
    status,
    expected_claim,
):
    repository, _sessions, diagnosis_id = postgres_repository
    action, _ = await repository.create(
        values(
            diagnosis_id,
            status=status,
            prepared_payload={"opaque": "snapshot"},
            updated_at=datetime.now(UTC) - timedelta(minutes=10),
        ),
        enqueue=False,
    )

    claim = await repository.claim_execution(action["id"], "recovery-worker")

    assert claim == expected_claim
    recovered = await repository.get(action["id"])
    assert recovered["status"] == ("reconciling" if claim == "reconcile" else "failed")
    if status == "rolling_back":
        assert recovered["completed_at"] is not None
        assert await repository.is_circuit_open("orders") is True


@pytest.mark.asyncio
async def test_postgres_claim_fencing_survives_takeover_after_service_read(postgres_repository):
    repository, sessions, diagnosis_id = postgres_repository
    second_repository = SQLAlchemyRemediationRepository(sessions)
    action, _ = await repository.create(values(diagnosis_id), enqueue=False)
    await repository.claim_execution(action["id"], "old-claim")
    original_get = repository.get

    async def takeover_after_read(action_id):
        snapshot = await original_get(action_id)
        await second_repository.compare_and_set(
            action_id,
            {"executing"},
            updated_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        await second_repository.claim_execution(action_id, "new-claim")
        repository.get = original_get
        return snapshot

    repository.get = takeover_after_read
    service = RemediationService(repository=repository)

    with pytest.raises(RemediationConflict):
        await service.apply_gateway_event(
            action["id"], GatewayEvent(status="verifying"), claim_token="old-claim"
        )
    persisted = await original_get(action["id"])
    assert persisted["worker_name"] == "new-claim"
    assert persisted["status"] == "executing"


@pytest.mark.asyncio
async def test_postgres_decision_after_expiry_sweep_remains_expired(postgres_repository):
    repository, _sessions, diagnosis_id = postgres_repository
    now = datetime.now(UTC)
    action, _ = await repository.create(
        values(
            diagnosis_id,
            status="awaiting_approval",
            approval_expires_at=now,
        ),
        enqueue=False,
    )
    await repository.expire_due_approvals(now)

    assert await repository.decide(action["id"], "approve", "operator", now) == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "approved"])
async def test_postgres_claim_rejects_legacy_snapshot(postgres_repository, status):
    repository, _sessions, diagnosis_id = postgres_repository
    action, _ = await repository.create(values(diagnosis_id, status=status, evidence_snapshot=[]), enqueue=False)
    assert await repository.claim_execution(action["id"], "worker") == "ignored"
    persisted = await repository.get(action["id"])
    assert persisted["status"] == "denied"
    assert persisted["error"] == "frozen_evidence_invalid_or_expired"


@pytest.mark.asyncio
async def test_postgres_approval_rechecks_frozen_snapshot(postgres_repository):
    repository, sessions, diagnosis_id = postgres_repository
    now = datetime.now(UTC)
    action, _ = await repository.create(values(diagnosis_id, status="awaiting_approval",
        evidence_snapshot=[], approval_expires_at=now + timedelta(minutes=10)), enqueue=False)
    assert await repository.decide(action["id"], "approve", "operator", now) == "expired"
    async with sessions() as session:
        assert await session.scalar(select(OutboxEvent).where(OutboxEvent.diagnosis_id == diagnosis_id)) is None
