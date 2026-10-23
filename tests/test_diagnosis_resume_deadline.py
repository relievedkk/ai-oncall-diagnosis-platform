"""Retries preserve pending work and solo workers honor diagnostic deadlines."""

import asyncio
import importlib

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.config import config
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.aiops_service import AIOpsService
from app.services.diagnosis_runner import DiagnosisRunner


async def create_job(repository, *, trigger="manual", retry=False):
    job_id = "resume-deadline-test"
    await repository.create_or_get_job(
        {
            "diagnosis_id": job_id,
            "event_key": job_id,
            "trigger": trigger,
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert": {
                "labels": {"alertname": "CPUHigh", "service": "ai-oncall-agent"},
                "annotations": {},
                "startsAt": "2026-10-15T00:00:00Z",
                "status": "firing",
            },
        }
    )
    if retry:
        await repository.claim_job(job_id, "old-task", "old-worker")
        await repository.update_job(job_id, status="interrupted")
    return job_id


def checkpoint_service(monkeypatch):
    module = importlib.import_module("app.services.aiops_service")

    async def planner(_state):
        return {"plan": ["replanned"]}

    async def executor(state):
        step = state["plan"][0]
        return {"plan": [], "past_steps": [(step, "real result")]}

    async def replanner(state):
        return {"response": ",".join(step for step, _ in state["past_steps"])}

    monkeypatch.setattr(module, "planner", planner)
    monkeypatch.setattr(module, "executor", executor)
    monkeypatch.setattr(module, "replanner", replanner)
    service = AIOpsService()
    service.checkpointer = MemorySaver()
    service.graph = service._build_graph(service.checkpointer)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["manual", "alertmanager"])
async def test_runner_retry_resumes_pending_checkpoint(monkeypatch, trigger):
    runner_module = importlib.import_module("app.services.diagnosis_runner")
    repository = InMemoryDiagnosisRepository()
    job_id = await create_job(repository, trigger=trigger, retry=True)
    service = checkpoint_service(monkeypatch)
    monkeypatch.setattr(runner_module, "aiops_service", service)
    await service.graph.aupdate_state(
        {"configurable": {"thread_id": f"aiops:{job_id}"}},
        {
            "input": "original task",
            "plan": ["pending step"],
            "past_steps": [("saved step", "saved evidence")],
            "evidence": [],
            "response": "",
        },
        as_node="planner",
    )
    assert await DiagnosisRunner(repository).run(job_id, "new-task", "worker") == "completed"
    job = await repository.get_job(job_id)
    assert job["report"] == "saved step,pending step"
    assert job["attempt_count"] == 2


@pytest.mark.asyncio
async def test_runner_retry_of_terminal_failure_starts_fresh(monkeypatch):
    runner_module = importlib.import_module("app.services.diagnosis_runner")
    repository = InMemoryDiagnosisRepository()
    job_id = await create_job(repository, retry=True)
    service = checkpoint_service(monkeypatch)
    monkeypatch.setattr(runner_module, "aiops_service", service)
    await service.graph.aupdate_state(
        {"configurable": {"thread_id": f"aiops:{job_id}"}},
        {
            "input": "old task",
            "plan": [],
            "past_steps": [("failed step", "failed")],
            "evidence": [],
            "response": "old partial report",
            "execution_errors": ["failed"],
        },
        as_node="replanner",
    )
    assert await DiagnosisRunner(repository).run(job_id, "new-task", "worker") == "completed"
    assert (await repository.get_job(job_id))["report"] == "replanned"


@pytest.mark.asyncio
@pytest.mark.parametrize("soft,hard", [(0.03, 1), (1, 0.03)])
async def test_runner_deadline_cancels_slow_work_and_records_failure(monkeypatch, soft, hard):
    runner_module = importlib.import_module("app.services.diagnosis_runner")
    repository = InMemoryDiagnosisRepository()
    job_id = await create_job(repository)
    cancelled = asyncio.Event()

    async def slow_diagnosis(session_id, **kwargs):
        try:
            yield {"type": "plan", "plan": ["slow query"]}
            await asyncio.Event().wait()
            yield {"type": "complete", "response": "late report"}
        finally:
            cancelled.set()

    monkeypatch.setattr(config, "celery_task_soft_time_limit", soft)
    monkeypatch.setattr(config, "celery_task_time_limit", hard)
    monkeypatch.setattr(runner_module.aiops_service, "diagnose", slow_diagnosis)
    with pytest.raises(TimeoutError, match="deadline"):
        await asyncio.wait_for(DiagnosisRunner(repository).run(job_id, "task", "worker"), 0.5)
    assert cancelled.is_set()
    job = await repository.get_job(job_id)
    assert job["status"] == "failed"
    assert "deadline" in job["error"].casefold()
    assert job["report"] == ""


@pytest.mark.asyncio
async def test_queue_managed_failure_does_not_expose_terminal_status_before_retry(monkeypatch):
    runner_module = importlib.import_module("app.services.diagnosis_runner")
    repository = InMemoryDiagnosisRepository()
    job_id = await create_job(repository)

    async def failure(**kwargs):
        yield {"type": "error", "message": "temporary provider failure"}

    monkeypatch.setattr(runner_module.aiops_service, "diagnose", failure)
    with pytest.raises(RuntimeError, match="temporary"):
        await DiagnosisRunner(repository).run(job_id, "task", "worker", retry_managed=True)
    assert (await repository.get_job(job_id))["status"] == "running"
    await repository.prepare_retry(job_id, "temporary provider failure")
    assert (await repository.get_job(job_id))["status"] == "retrying"


@pytest.mark.asyncio
async def test_stream_survives_attempt_error_and_waits_for_committed_completion():
    from app.services.alert_diagnosis_service import AlertDiagnosisService

    class Repository:
        calls = 0

        async def get_job(self, _):
            self.calls += 1
            return {
                "status": "retrying" if self.calls == 1 else "completed",
                "attempt_count": 2,
                "report": "final report",
                "evidence": [
                    {"id": 1, "payload": {"type": "error", "attempt": 1, "message": "temporary"}},
                    {
                        "id": 2,
                        "payload": {"type": "complete", "attempt": 2, "response": "final report"},
                    },
                ],
            }

    repository = Repository()
    events = [
        event
        async for event in AlertDiagnosisService(repository, inline_worker=False).stream_job("test")
    ]
    assert repository.calls == 2
    assert all(event["type"] not in {"error", "complete"} for event in events[:-1])
    assert events[-1]["type"] == "complete"
    assert events[-1]["response"] == "final report"


@pytest.mark.asyncio
async def test_stream_emits_terminal_failure_even_without_error_evidence():
    from app.services.alert_diagnosis_service import AlertDiagnosisService

    class Repository:
        async def get_job(self, _):
            return {"status": "dead_lettered", "error": "retries exhausted", "evidence": []}

    events = [
        event
        async for event in AlertDiagnosisService(Repository(), inline_worker=False).stream_job(
            "test"
        )
    ]
    assert events == [
        {
            "type": "error",
            "stage": "dead_lettered",
            "message": "retries exhausted",
            "diagnosis_id": "test",
        }
    ]
