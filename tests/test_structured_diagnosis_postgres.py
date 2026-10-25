"""Structured reports follow the same database authority as Markdown."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import DiagnosisJob
from app.repositories.diagnosis import DiagnosisLeaseLostError, SQLAlchemyDiagnosisRepository
from app.services.alert_diagnosis_service import AlertDiagnosisService

pytestmark = pytest.mark.integration


async def test_postgres_json_result_roundtrip_reconnect_and_stale_owner_rejection():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("explicit TEST_DATABASE_URL required")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyDiagnosisRepository(sessions)
    job_id = "structured-test-" + uuid4().hex
    try:
        await repository.create_or_get_job(
            {"diagnosis_id": job_id, "event_key": job_id, "service": "orders", "status": "queued"}
        )
        await repository.claim_job(job_id, "task", "worker", execution_token="current")
        structured = {
            "schema_version": 1,
            "outcome": "insufficient_evidence",
            "summary": "missing logs",
        }
        await repository.record_event(
            job_id,
            {"type": "report", "report": "partial", "diagnosis_result": structured},
            execution_token="current",
        )
        with pytest.raises(DiagnosisLeaseLostError):
            await repository.record_event(
                job_id,
                {"type": "report", "report": "stale", "diagnosis_result": {"outcome": "diagnosed"}},
                execution_token="stale",
            )
        await repository.update_job(job_id, status="completed", execution_token="current")
        assert (await repository.get_job(job_id))["diagnosis_result"] == structured
        cursor = max(item["id"] for item in (await repository.get_job(job_id))["evidence"])
        page = await repository.get_job_stream(job_id, after_id=cursor)
        assert page["evidence"] == [] and page["diagnosis_result"] == structured
        service = AlertDiagnosisService(repository=repository, inline_worker=False)
        events = [event async for event in service.stream_job(job_id, after_id=cursor)]
        assert events[-1]["diagnosis_result"] == structured
        assert events[-1]["response"] == "partial"
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(DiagnosisJob).where(DiagnosisJob.diagnosis_id == job_id))
        await engine.dispose()
