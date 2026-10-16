"""Execution ownership must survive broker redelivery of the same task ID."""

from datetime import UTC, datetime, timedelta

import pytest

from app.repositories import diagnosis


async def queued_job(repository, diagnosis_id="ownership-test"):
    await repository.create_or_get_job_and_enqueue(
        {
            "diagnosis_id": diagnosis_id,
            "event_key": diagnosis_id,
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert": {},
        },
        "diagnosis.default",
    )
    return diagnosis_id


@pytest.mark.asyncio
async def test_redelivered_task_rejects_all_stale_execution_writes(monkeypatch):
    now = datetime(2026, 10, 15, tzinfo=UTC)
    monkeypatch.setattr(diagnosis, "utc_now", lambda: now)
    repository = diagnosis.InMemoryDiagnosisRepository()
    job_id = await queued_job(repository)
    assert await repository.claim_job(job_id, "same-task", "worker-a", execution_token="a") == "claimed"
    now += timedelta(seconds=1000)
    assert await repository.claim_job(job_id, "same-task", "worker-b", execution_token="b") == "claimed"
    assert await repository.heartbeat(job_id, "same-task", execution_token="a") is False
    for operation in (
        repository.record_event(job_id, {"type": "report", "report": "stale report"}, execution_token="a"),
        repository.update_job(job_id, status="failed", execution_token="a"),
        repository.skip_remaining_steps(job_id, "stale completion", execution_token="a"),
        repository.prepare_retry(job_id, "stale retry", execution_token="a"),
        repository.mark_dead_lettered(job_id, "stale terminal", execution_token="a"),
    ):
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            await operation
    job = await repository.get_job(job_id)
    assert job["status"] == "running"
    assert job["worker_name"] == "worker-b"
    assert job["report"] == ""
    assert job["evidence"] == []
    assert await repository.heartbeat(job_id, "same-task", execution_token="b") is True
    await repository.record_event(job_id, {"type": "report", "report": "current report"}, execution_token="b")
    await repository.update_job(job_id, status="completed", execution_token="b")
    assert (await repository.get_job(job_id))["report"] == "current report"


@pytest.mark.asyncio
async def test_expired_lease_cannot_renew_even_before_another_worker_claims(monkeypatch):
    now = datetime(2026, 10, 15, tzinfo=UTC)
    monkeypatch.setattr(diagnosis, "utc_now", lambda: now)
    repository = diagnosis.InMemoryDiagnosisRepository()
    job_id = await queued_job(repository)
    await repository.claim_job(job_id, "task", "worker", execution_token="owner")
    now += timedelta(seconds=1000)
    assert await repository.heartbeat(job_id, "task", execution_token="owner") is False
    with pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await repository.record_event(job_id, {"type": "complete", "response": "late"}, execution_token="owner")


@pytest.mark.asyncio
async def test_runner_cancels_work_when_heartbeat_loses_ownership(monkeypatch):
    import asyncio

    from app.config import config
    from app.services import diagnosis_runner

    repository = diagnosis.InMemoryDiagnosisRepository()
    job_id = await queued_job(repository)
    stopped = asyncio.Event()

    async def slow_diagnosis(session_id, **kwargs):
        try:
            await asyncio.sleep(10)
            yield {"type": "complete", "response": "must not finish"}
        finally:
            stopped.set()

    async def lost_heartbeat(*args, **kwargs):
        return False

    monkeypatch.setattr(config, "diagnosis_heartbeat_seconds", 0.01)
    monkeypatch.setattr(repository, "heartbeat", lost_heartbeat)
    monkeypatch.setattr(diagnosis_runner.aiops_service, "diagnose", slow_diagnosis)
    with pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await asyncio.wait_for(diagnosis_runner.DiagnosisRunner(repository).run(job_id, "task", "worker"), 1)
    assert stopped.is_set()
    job = await repository.get_job(job_id)
    assert job["status"] == "running"
    assert not job["evidence"]


@pytest.mark.parametrize("result", [
    [{"type": "text", "text": '{"success":false,"error":"unavailable"}'}],
    [{"type": "text", "text": "failed after retries"}],
    {"success": True, "data_status": "no_data", "data_points": []},
    {"success": True, "data_status": "no_traffic", "data_points": []},
    None,
])
def test_failed_or_empty_metric_results_are_not_successful_policy_evidence(result):
    from app.services.diagnosis_runner import _remediation_evidence

    evidence = _remediation_evidence({"evidence": [{"evidence_type": "metric", "source": "query_cpu_metrics", "payload": {"result": result}}]})
    assert evidence[0]["success"] is False


def test_valid_mcp_text_metric_preserves_success_and_payload():
    from app.services.diagnosis_runner import _remediation_evidence

    evidence = _remediation_evidence({"evidence": [{"evidence_type": "metric", "source": "query_cpu_metrics", "payload": {"result": [{"type": "text", "text": '{"success":true,"data_status":"available","data_points":[{"timestamp":"2026-10-16T00:00:00Z","value":90}]}'}]}}]})
    assert evidence[0]["success"] is True
    assert evidence[0]["payload"]["data_points"] == [{"timestamp": "2026-10-16T00:00:00Z", "value": 90}]


@pytest.mark.asyncio
async def test_dead_letter_requires_explicit_atomic_replay():
    import asyncio
    repository = diagnosis.InMemoryDiagnosisRepository()
    job_id = await queued_job(repository)
    await repository.claim_job(job_id, "old", "worker", execution_token="old")
    await repository.mark_dead_lettered(job_id, "failure", execution_token="old")
    assert await repository.claim_job(job_id, "redelivery", "worker") == "dead_lettered"
    results = await asyncio.gather(*(repository.requeue_dead_lettered(job_id, "diagnosis.default") for _ in range(2)))
    assert sorted(results) == [False, True]
    assert len(repository._outbox) == 2
    job = await repository.get_job(job_id)
    assert job["execution_token"] is None
    assert job["dead_lettered_at"] is None
    assert await repository.claim_job(job_id, "replay", "worker", execution_token="new") == "claimed"


@pytest.mark.asyncio
async def test_dead_letter_duplicate_is_acknowledged_without_starting_graph():
    from app.services.diagnosis_runner import DiagnosisRunner
    repository = diagnosis.InMemoryDiagnosisRepository()
    job_id = await queued_job(repository)
    await repository.claim_job(job_id, "old", "worker", execution_token="old")
    await repository.mark_dead_lettered(job_id, "failure", execution_token="old")
    assert await DiagnosisRunner(repository).run(job_id, "duplicate", "worker") == "already_dead_lettered"
    assert (await repository.get_job(job_id))["attempt_count"] == 1
