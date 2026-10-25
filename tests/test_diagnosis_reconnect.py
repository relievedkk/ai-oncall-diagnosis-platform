"""Durable creation and subscription behavior; no model or network calls."""

import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import aiops
from app.db.models import DiagnosisJob, OutboxEvent
from app.repositories.diagnosis import (
    InMemoryDiagnosisRepository,
    SQLAlchemyDiagnosisRepository,
    utc_now,
)
from app.services.alert_diagnosis_service import AlertDiagnosisService


@pytest.fixture
def service():
    return AlertDiagnosisService(InMemoryDiagnosisRepository(), inline_worker=False)


@pytest.fixture
def api(service, monkeypatch):
    monkeypatch.setattr(aiops, "alert_diagnosis_service", service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.principal = SimpleNamespace(user_id=request.headers.get("user", "alice"),
                                                  role=request.headers.get("role", "operator"))
        return await call_next(request)

    app.include_router(aiops.router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def test_concurrent_idempotency_scoped_to_creator_and_payload_conflict(api, service):
    async with api:
        results = await asyncio.gather(*[
            api.post("/api/aiops/jobs", headers={"Idempotency-Key": "retry-key"},
                     json={"session_id": "session-1"}) for _ in range(12)
        ])
        assert {r.status_code for r in results} == {202}
        assert len({r.json()["diagnosis_id"] for r in results}) == 1
        assert results[0].json()["events_url"].endswith("/events")
        conflict = await api.post("/api/aiops/jobs", headers={"Idempotency-Key": "retry-key"},
                                  json={"session_id": "session-2"})
        assert conflict.status_code == 409
        other = await api.post("/api/aiops/jobs", headers={"Idempotency-Key": "retry-key", "user": "bob"},
                               json={"session_id": "session-1"})
        assert other.status_code == 202
        assert other.json()["diagnosis_id"] != results[0].json()["diagnosis_id"]
    assert len(await service.repository.claim_outbox("publisher")) == 2


async def test_manual_subscription_creator_only_legacy_admin_and_alert_team(api, service):
    async with api:
        response = await api.post("/api/aiops/jobs", headers={"Idempotency-Key": "owner-key"}, json={})
        assert response.status_code == 202
        job_id = response.json()["diagnosis_id"]
        await service.repository.update_job(job_id, status="completed")
        assert (await api.get(response.json()["events_url"], headers={"user": "bob"})).status_code == 403
        assert (await api.get(response.json()["events_url"])).status_code == 200
        legacy = await service.enqueue_manual("legacy")
        await service.repository.update_job(legacy, status="completed")
        assert (await api.get(f"/api/aiops/jobs/{legacy}/events")).status_code == 403
        assert (await api.get(f"/api/aiops/jobs/{legacy}/events", headers={"role": "admin"})).status_code == 200
        service.repository._jobs[legacy]["trigger"] = "alertmanager"
        assert (await api.get(f"/api/aiops/jobs/{legacy}/events")).status_code == 200


async def test_incremental_stream_real_ids_stable_terminal_and_header_cursor(api, service):
    async with api:
        created = await api.post("/api/aiops/jobs", headers={"Idempotency-Key": "stream-key"}, json={})
        assert created.status_code == 202
        job_id = created.json()["diagnosis_id"]
        repo = service.repository
        await repo.record_event(job_id, {"type": "status", "message": "中文"})
        await repo.record_event(job_id, {"type": "report", "report": "final report", "attempt": 1})
        await repo.record_event(job_id, {"type": "complete", "response": "final report", "attempt": 1})
        await repo.update_job(job_id, status="completed", attempt_count=1)
        job = await repo.get_job(job_id)
        events = [event async for event in service.stream_job(job_id, after_id=job["evidence"][0]["id"])]
        assert events[0]["_event_id"] == str(job["evidence"][1]["id"])
        assert events[-1]["_event_id"] == "terminal:1:completed"
        replay = [event async for event in service.stream_job(job_id, after_id=job["evidence"][-1]["id"])]
        assert replay[-1]["_event_id"] == events[-1]["_event_id"]
        assert replay[-1]["response"] == "final report"
        assert [e async for e in service.stream_job(job_id, after_id=events[-1]["_event_id"])] == []
        response = await api.get(created.json()["events_url"], headers={"Last-Event-ID": str(job["evidence"][-1]["id"])})
        assert "id: terminal:1:completed" in response.text
        assert (await api.get(created.json()["events_url"] + "?after_id=-1")).status_code == 422
        assert len(await repo.claim_outbox("publisher")) == 1


async def test_old_attempt_complete_cannot_override_authoritative_report(service):
    job_id = await service.enqueue_manual("attempts")
    repo = service.repository
    await repo.record_event(job_id, {"type": "complete", "response": "old", "attempt": 1})
    await repo.record_event(job_id, {"type": "report", "report": "new", "attempt": 2})
    await repo.update_job(job_id, status="completed", attempt_count=2)
    events = [e async for e in service.stream_job(job_id)]
    assert events[-1]["response"] == "new"
    assert events[-1]["_event_id"] == "terminal:2:completed"


async def test_oldest_waiting_age_only_queued_or_retrying(service):
    old = await service.enqueue_manual("old")
    new = await service.enqueue_manual("new")
    repo = service.repository
    await repo.update_job(old, status="running", created_at=(utc_now() - timedelta(hours=3)).isoformat())
    await repo.update_job(new, status="retrying", created_at=(utc_now() - timedelta(seconds=50)).isoformat())
    metrics = await repo.get_operational_metrics()
    assert 50 <= metrics["oldest_waiting_seconds"] < 55


@pytest.mark.integration
async def test_postgres_independent_sessions_create_one_task_and_outbox():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured; real PostgreSQL concurrency unverified")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner = f"test-reconnect:{uuid4().hex}"
    try:
        # Every repository call opens its own transaction; no process lock can
        # make this pass instead of the persisted unique event_key constraint.
        services = [AlertDiagnosisService(SQLAlchemyDiagnosisRepository(sessions), inline_worker=False)
                    for _ in range(8)]
        jobs = await asyncio.gather(*[
            service.enqueue_manual("test-session", owner=owner, idempotency_key="same-key")
            for service in services
        ])
        assert len(set(jobs)) == 1
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(DiagnosisJob).where(
                DiagnosisJob.diagnosis_id == jobs[0])) == 1
            assert await session.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.diagnosis_id == jobs[0])) == 1
        with pytest.raises(ValueError, match="another payload"):
            await services[0].enqueue_manual("different", owner=owner, idempotency_key="same-key")
        await services[0].repository.update_job(jobs[0], status="completed")
        # A separately instantiated service observes the persisted report/cursor.
        events = [event async for event in services[1].stream_job(jobs[0])]
        assert events[-1]["_event_id"] == "terminal:0:completed"
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(DiagnosisJob).where(
                DiagnosisJob.alert_payload["owner"].as_string() == owner))
        await engine.dispose()
