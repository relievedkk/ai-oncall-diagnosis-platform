import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import DiagnosisJob, OutboxEvent, RemediationAction
from app.repositories.remediation import SQLAlchemyRemediationRepository

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
        "evidence_snapshot": [],
        "policy_decision": "allow",
        "policy_reason": "policy_allowed",
        "requested_by": "agent",
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


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
