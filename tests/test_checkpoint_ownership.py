"""A replaced diagnosis must not modify LangGraph state or pending writes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import MemorySaver

from app.config import config
from app.core import checkpoint as storage
from app.repositories import diagnosis


def scope(repository, token, job_id="checkpoint-owner"):
    return storage.diagnosis_checkpoint_scope(repository, job_id, token)


def checkpoint_config(thread="aiops:checkpoint-owner"):
    return {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}


async def save(saver, value, config=None):
    state = empty_checkpoint()
    state["channel_values"] = {"value": value}
    state["channel_versions"] = {"value": 1}
    return await saver.aput(
        config or checkpoint_config(), state,
        {"source": "loop", "step": 1, "parents": {}}, {"value": 1},
    )


@pytest.fixture
async def ownership(monkeypatch):
    now = datetime.now(UTC)
    clock = [now]
    monkeypatch.setattr(diagnosis, "utc_now", lambda: clock[0])
    monkeypatch.setattr(config, "langgraph_checkpoint_enabled", False)
    repository = diagnosis.InMemoryDiagnosisRepository()
    await repository.create_or_get_job({
        "diagnosis_id": "checkpoint-owner", "event_key": "checkpoint-owner", "status": "queued",
    })
    await repository.claim_job("checkpoint-owner", "same-task", "a", execution_token="a")
    manager = storage.CheckpointManager()
    saver = await manager.initialize()
    yield repository, saver, clock
    await manager.close()


async def replace_owner(repository, clock):
    clock[0] += timedelta(seconds=1000)
    assert await repository.claim_job("checkpoint-owner", "same-task", "b", execution_token="b") == "claimed"


@pytest.mark.asyncio
async def test_replaced_worker_cannot_overwrite_checkpoint(ownership):
    repository, saver, clock = ownership
    with scope(repository, "a"):
        await save(saver, "first")
    await replace_owner(repository, clock)
    with scope(repository, "b"):
        await save(saver, "replacement")
    with scope(repository, "a"), pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await save(saver, "stale overwrite")
    assert (await saver.aget_tuple(checkpoint_config())).checkpoint["channel_values"]["value"] == "replacement"


@pytest.mark.asyncio
async def test_replaced_worker_cannot_write_pending_results_or_delete_state(ownership):
    repository, saver, clock = ownership
    with scope(repository, "a"):
        saved = await save(saver, "first")
    await replace_owner(repository, clock)
    with scope(repository, "a"):
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            await saver.aput_writes(saved, [("value", "stale tool result")], "task")
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            await saver.adelete_thread("aiops:checkpoint-owner")
    assert (await saver.aget_tuple(saved)).pending_writes == []


@pytest.mark.asyncio
async def test_expired_owner_cannot_write_before_replacement_claims(ownership):
    repository, saver, clock = ownership
    clock[0] += timedelta(seconds=1000)
    with scope(repository, "a"), pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await save(saver, "expired")


@pytest.mark.asyncio
async def test_diagnosis_namespace_requires_scope_but_chat_still_works(ownership):
    _, saver, _ = ownership
    with pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await save(saver, "unowned")
    chat = await save(saver, "chat", checkpoint_config("chat:user:1:session"))
    assert (await saver.aget_tuple(chat)).checkpoint["channel_values"]["value"] == "chat"


@pytest.mark.asyncio
async def test_scope_cannot_write_another_thread(ownership):
    repository, saver, _ = ownership
    with scope(repository, "a"), pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await save(saver, "wrong target", checkpoint_config("aiops:another-job"))


@pytest.mark.asyncio
async def test_owner_change_waits_for_checkpoint_write_to_finish(ownership):
    repository, _, clock = ownership
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedSaver(MemorySaver):
        async def aput(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().aput(*args, **kwargs)

    saver = storage.FencedCheckpointSaver(PausedSaver())

    async def writing():
        with scope(repository, "a"):
            return await save(saver, "before takeover")

    writer = asyncio.create_task(writing())
    await entered.wait()
    clock[0] += timedelta(seconds=1000)
    claimant = asyncio.create_task(repository.claim_job("checkpoint-owner", "same-task", "b", execution_token="b"))
    try:
        await asyncio.sleep(0)
        assert not claimant.done(), "claim must wait for the in-flight guarded write"
    finally:
        release.set()
        await asyncio.gather(writer, claimant)
    assert claimant.result() == "claimed"


@pytest.mark.asyncio
async def test_allowlist_clone_and_sync_api_cannot_bypass_ownership(ownership):
    repository, saver, clock = ownership
    clone = saver.with_allowlist([("example", "Record")])
    with scope(repository, "a"):
        saved = await save(clone, "first")
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            clone.put_writes(saved, [("value", "sync bypass")], "sync")
    await replace_owner(repository, clock)
    with scope(repository, "a"), pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await save(clone, "allowlist bypass")


@pytest.mark.asyncio
async def test_real_graph_takeover_preserves_replacement_report(monkeypatch, ownership):
    import importlib

    from app.services.aiops_service import AIOpsService
    from app.services.diagnosis_runner import DiagnosisRunner

    repository, saver, clock = ownership
    # Let the Runner perform the first claim, as it does for a new broker task.
    await repository.update_job("checkpoint-owner", status="queued")
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    calls = []

    async def planner(_state):
        return {"plan": ["inspect"]}

    async def executor(_state):
        calls.append(len(calls))
        first = len(calls) == 1
        if first:
            first_entered.set()
            await release_first.wait()
        return {"plan": [], "past_steps": [("inspect", "stale" if first else "replacement")]}

    async def replanner(state):
        return {"response": state["past_steps"][-1][1]}

    module = importlib.import_module("app.services.aiops_service")
    for name, node in (("planner", planner), ("executor", executor), ("replanner", replanner)):
        monkeypatch.setattr(module, name, node)
    service = AIOpsService()
    service.checkpointer = saver
    service.graph = service._build_graph(saver)
    runner_module = importlib.import_module("app.services.diagnosis_runner")
    monkeypatch.setattr(runner_module, "aiops_service", service)
    monkeypatch.setattr(config, "diagnosis_heartbeat_seconds", 10000)
    job = repository._jobs["checkpoint-owner"]
    job.update(trigger="manual", alert={}, service="manual")
    stale = asyncio.create_task(DiagnosisRunner(repository).run("checkpoint-owner", "same-task", "a", execution_token="a"))
    await asyncio.wait_for(first_entered.wait(), 2)
    clock[0] += timedelta(seconds=1000)
    try:
        assert await DiagnosisRunner(repository).run("checkpoint-owner", "same-task", "b", execution_token="b") == "completed"
    finally:
        release_first.set()
    with pytest.raises(diagnosis.DiagnosisLeaseLostError):
        await stale
    current = await service.graph.aget_state(checkpoint_config())
    assert current.values["response"] == "replacement"
    assert (await repository.get_job("checkpoint-owner"))["report"] == "replacement"


@pytest.mark.asyncio
async def test_runner_closes_stream_before_leaving_owner_scope(monkeypatch, ownership):
    import importlib

    from app.core.checkpoint_fencing import _owner
    from app.services.diagnosis_runner import DiagnosisRunner

    repository, _, _ = ownership
    await repository.update_job("checkpoint-owner", status="queued")
    repository._jobs["checkpoint-owner"].update(trigger="manual", alert={}, service="manual")
    cleanup = []

    async def stream():
        try:
            yield {"type": "plan", "plan": ["inspect"]}
        finally:
            current = _owner.get()
            cleanup.append(current.execution_token if current else None)

    events = stream()

    async def lost(*args, **kwargs):
        raise diagnosis.DiagnosisLeaseLostError("replacement claimed the job")

    runner_module = importlib.import_module("app.services.diagnosis_runner")
    monkeypatch.setattr(runner_module.aiops_service, "diagnose", lambda **kwargs: events)
    monkeypatch.setattr(repository, "record_event", lost)
    try:
        with pytest.raises(diagnosis.DiagnosisLeaseLostError):
            await DiagnosisRunner(repository).run("checkpoint-owner", "task", "a", execution_token="a")
        assert cleanup == ["a"], "stream cleanup must be awaited within its execution scope"
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_manual_diagnosis_close_reaches_inner_execution(monkeypatch):
    from app.services.aiops_service import AIOpsService

    closed = []

    async def execute(*args, **kwargs):
        try:
            yield {"type": "plan", "plan": ["inspect"]}
        finally:
            closed.append(True)

    inner = execute()
    service = AIOpsService()
    monkeypatch.setattr(service, "execute", lambda *args, **kwargs: inner)
    outer = service.diagnose("scope-close")
    try:
        await anext(outer)
        await outer.aclose()
        assert closed == [True]
    finally:
        await inner.aclose()
