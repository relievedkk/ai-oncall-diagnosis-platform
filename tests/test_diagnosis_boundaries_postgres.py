"""Diagnosis budget and bounded reads against an explicitly disposable database."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import DiagnosisJob, OutboxEvent
from app.models.alerts import AlertmanagerWebhook
from app.repositories.diagnosis import SQLAlchemyDiagnosisRepository
from app.services.alert_diagnosis_service import AlertDiagnosisService

pytestmark = pytest.mark.integration


@pytest.fixture
async def boundary_postgres():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyDiagnosisRepository(sessions)
    ids = []
    try:
        yield repository, sessions, ids
    finally:
        if ids:
            async with sessions.begin() as session:
                await session.execute(delete(DiagnosisJob).where(DiagnosisJob.diagnosis_id.in_(ids)))
        await engine.dispose()


async def test_postgres_budget_reservation_is_atomic_and_survives_retry(boundary_postgres):
    repository, sessions, ids = boundary_postgres
    job_id = "test-boundary-" + uuid4().hex
    ids.append(job_id)
    await repository.create_or_get_job({"diagnosis_id": job_id, "event_key": job_id, "trigger": "manual"})
    await repository.claim_job(job_id, "task", "worker", execution_token="first")
    async def reserve(token="first", size=3):
        return await repository.reserve_model_budget(job_id, execution_token=token, input_bytes=size,
                                                    max_calls=3, max_input_bytes=8)
    assert sorted(await asyncio.gather(*(reserve() for _ in range(4)))) == [False, False, True, True]
    await repository.prepare_retry(job_id, "retry", execution_token="first")
    await repository.claim_job(job_id, "task", "worker", execution_token="second")
    assert not await reserve("first", 1)
    assert await reserve("second", 2)
    assert not await reserve("second", 0)
    # A new repository/session must observe counters charged by the old owner.
    reloaded = await SQLAlchemyDiagnosisRepository(sessions).get_job(job_id)
    assert (reloaded["model_calls"], reloaded["model_input_bytes"]) == (3, 8)


async def test_postgres_equivalent_legacy_fingerprintless_identity_and_resolve(boundary_postgres):
    repository, sessions, ids = boundary_postgres
    service = AlertDiagnosisService(repository, inline_worker=False)
    job_id = "test-boundary-" + uuid4().hex
    ids.append(job_id)
    labels = {"alertname": "BoundaryTest", "service": "test", "region": uuid4().hex}
    original = {"status": "firing", "labels": labels, "startsAt": "2026-10-23T08:00:00+08:00", "endsAt": "", "fingerprint": ""}
    await repository.create_or_get_job_and_enqueue({
        "diagnosis_id": job_id, "event_key": job_id, "trigger": "alertmanager", "alert": original,
    }, "test")
    equivalent = {**original, "startsAt": "2026-10-23T00:00:00.000Z"}
    assert await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [equivalent]})) == [job_id]
    newer = {**original, "startsAt": "2026-10-23T01:00:00Z"}
    newer_id = (await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [newer]})))[0]
    ids.append(newer_id)
    resolved = {**equivalent, "status": "resolved", "endsAt": "2026-10-23T00:10:00Z"}
    await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [resolved]}))
    assert (await repository.get_job(job_id))["alert_status"] == "resolved"
    assert (await repository.get_job(newer_id))["alert_status"] == "firing"
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.diagnosis_id == job_id))
        assert count == 1


async def test_postgres_bounded_summaries_and_stream_cursor(boundary_postgres):
    repository, _sessions, ids = boundary_postgres
    service = AlertDiagnosisService(repository, inline_worker=False)
    job_id = await service.enqueue_manual("test-boundary")
    ids.append(job_id)
    for index in range(105):
        await repository.record_event(job_id, {"type": "status", "index": index})
    await repository.update_job(job_id, status="completed")
    summaries = await repository.list_jobs(limit=1)
    assert len(summaries) == 1
    assert not {"evidence", "events", "steps", "alert", "report"} & summaries[0].keys()
    first = await repository.get_job_stream(job_id, limit=100)
    assert len(first["evidence"]) == 100 and first["has_more"]
    second = await repository.get_job_stream(job_id, after_id=first["evidence"][-1]["id"], limit=100)
    assert len(second["evidence"]) == 6 and not second["has_more"]
    events = [event async for event in service.stream_job(job_id)]
    assert [event["index"] for event in events if "index" in event] == list(range(105))
    assert events[-1]["type"] == "complete"
