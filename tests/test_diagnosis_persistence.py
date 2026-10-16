"""Diagnosis delivery and write fencing against a disposable PostgreSQL DB."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import DiagnosisJob, OutboxEvent
from app.repositories import diagnosis

pytestmark = pytest.mark.integration


@pytest.fixture
async def postgres_diagnosis():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = diagnosis.SQLAlchemyDiagnosisRepository(sessions)
    job_id = "test-diagnosis-" + uuid4().hex
    await repository.create_or_get_job_and_enqueue(
        {"diagnosis_id": job_id, "event_key": job_id, "trigger": "manual", "status": "queued", "alert_status": "not_applicable", "service": "manual", "alert": {}},
        "diagnosis.default",
    )
    try:
        yield repository, sessions, job_id
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(DiagnosisJob).where(DiagnosisJob.diagnosis_id == job_id))
        await engine.dispose()


async def test_postgres_duplicate_request_has_one_outbox(postgres_diagnosis):
    repository, sessions, job_id = postgres_diagnosis
    duplicate, created = await repository.create_or_get_job_and_enqueue(
        {"diagnosis_id": job_id, "event_key": job_id, "service": "manual"}, "diagnosis.default"
    )
    assert not created
    assert duplicate["diagnosis_id"] == job_id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.diagnosis_id == job_id)) == 1


async def test_postgres_stale_execution_cannot_write_or_retry(postgres_diagnosis, monkeypatch):
    repository, _sessions, job_id = postgres_diagnosis
    now = datetime.now(UTC)
    monkeypatch.setattr(diagnosis, "utc_now", lambda: now)
    assert await repository.claim_job(job_id, "same-task", "a", execution_token="a") == "claimed"
    now += timedelta(seconds=1000)
    assert await repository.claim_job(job_id, "same-task", "b", execution_token="b") == "claimed"
    assert not await repository.heartbeat(job_id, "same-task", execution_token="a")
    for operation in (
        repository.record_event(job_id, {"type": "report", "report": "old"}, execution_token="a"),
        repository.update_job(job_id, status="completed", execution_token="a"),
        repository.prepare_retry(job_id, "old", execution_token="a"),
        repository.mark_dead_lettered(job_id, "old", execution_token="a"),
        repository.skip_remaining_steps(job_id, "old", execution_token="a"),
    ):
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            await operation
    job = await repository.get_job(job_id)
    assert job["status"] == "running"
    assert job["evidence"] == []
    assert job["report"] == ""
    assert await repository.heartbeat(job_id, "same-task", execution_token="b")
    await repository.record_event(job_id, {"type": "report", "report": "current"}, execution_token="b")
    await repository.update_job(job_id, status="completed", execution_token="b")
    assert (await repository.get_job(job_id))["report"] == "current"


async def test_postgres_concurrent_claim_has_one_owner(postgres_diagnosis):
    repository, _sessions, job_id = postgres_diagnosis
    claims = await asyncio.gather(
        repository.claim_job(job_id, "task", "a", execution_token="a"),
        repository.claim_job(job_id, "task", "b", execution_token="b"),
    )
    assert sorted(claims) == ["busy", "claimed"]
    assert (await repository.get_job(job_id))["attempt_count"] == 1


async def test_postgres_dead_letter_replay_is_atomic(postgres_diagnosis):
    repository, sessions, job_id = postgres_diagnosis
    await repository.claim_job(job_id, "old", "worker", execution_token="old")
    await repository.mark_dead_lettered(job_id, "failure", execution_token="old")
    assert await repository.claim_job(job_id, "duplicate", "worker") == "dead_lettered"
    results = await asyncio.gather(*(repository.requeue_dead_lettered(job_id, "diagnosis.default") for _ in range(2)))
    assert sorted(results) == [False, True]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.diagnosis_id == job_id)) == 2
    assert await repository.claim_job(job_id, "replay", "worker", execution_token="new") == "claimed"
    assert not await repository.heartbeat(job_id, "old", execution_token="old")


async def test_operator_cli_requeues_dead_letter_once(postgres_diagnosis):
    import subprocess
    import sys
    repository, _sessions, job_id = postgres_diagnosis
    await repository.claim_job(job_id, "old", "worker", execution_token="old")
    await repository.mark_dead_lettered(job_id, "failure", execution_token="old")
    environment = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    command = [sys.executable, "-m", "app.tasks.replay_dead_letter", job_id]
    first = await asyncio.to_thread(subprocess.run, command, env=environment, capture_output=True, text=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert (await repository.get_job(job_id))["status"] == "queued"
    duplicate = await asyncio.to_thread(subprocess.run, command, env=environment, capture_output=True, text=True, timeout=20)
    assert duplicate.returncode == 1
