"""Checkpoint ownership is checked in the actual storage transaction."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.checkpoint_fencing import FencedCheckpointSaver, diagnosis_checkpoint_scope
from app.db.models import DiagnosisJob
from app.repositories.diagnosis import DiagnosisLeaseLostError, SQLAlchemyDiagnosisRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def durable_owner():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyDiagnosisRepository(sessions)
    job_id = "checkpoint-" + uuid4().hex
    await repository.create_or_get_job({"diagnosis_id": job_id, "event_key": job_id, "trigger": "manual", "status": "queued", "service": "manual", "alert": {}})
    await repository.claim_job(job_id, "same-task", "a", execution_token="a")
    config = {"configurable": {"thread_id": f"aiops:{job_id}", "checkpoint_ns": ""}}
    async with AsyncPostgresSaver.from_conn_string(url.replace("postgresql+asyncpg://", "postgresql://")) as raw:
        await raw.setup()
        try:
            yield repository, sessions, job_id, FencedCheckpointSaver(raw), raw, config
        finally:
            await raw.adelete_thread(config["configurable"]["thread_id"])
            async with sessions.begin() as session:
                await session.execute(delete(DiagnosisJob).where(DiagnosisJob.diagnosis_id == job_id))
    await engine.dispose()


async def write(saver, config, value):
    state = empty_checkpoint()
    state["channel_values"] = {"value": value}
    state["channel_versions"] = {"value": 1}
    return await saver.aput(config, state, {"source": "loop", "step": 1, "parents": {}}, {"value": 1})


async def expire(sessions, job_id):
    async with sessions.begin() as session:
        job = await session.get(DiagnosisJob, job_id, with_for_update=True)
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_postgres_replacement_rejects_checkpoint_and_pending_write(durable_owner):
    repository, sessions, job_id, saver, _, config = durable_owner
    with diagnosis_checkpoint_scope(repository, job_id, "a"):
        old = await write(saver, config, "first")
    await expire(sessions, job_id)
    assert await repository.claim_job(job_id, "same-task", "b", execution_token="b") == "claimed"
    with diagnosis_checkpoint_scope(repository, job_id, "b"):
        await write(saver, config, "replacement")
    with diagnosis_checkpoint_scope(repository, job_id, "a"):
        with pytest.raises(DiagnosisLeaseLostError):
            await write(saver, config, "stale")
        with pytest.raises(DiagnosisLeaseLostError):
            await saver.aput_writes(old, [("value", "stale pending result")], "old-task")
        with pytest.raises(DiagnosisLeaseLostError):
            await saver.adelete_thread(config["configurable"]["thread_id"])
    assert (await saver.aget_tuple(config)).checkpoint["channel_values"]["value"] == "replacement"
    assert (await saver.aget_tuple(old)).pending_writes == []


async def test_checkpoint_insert_rolls_back_with_ownership_transaction(durable_owner, monkeypatch):
    repository, _, job_id, saver, raw, config = durable_owner
    original = repository.checkpoint_write_guard

    @asynccontextmanager
    async def interrupted(*args, **kwargs):
        async with original(*args, **kwargs):
            yield
            raise RuntimeError("crash before ownership transaction commit")

    monkeypatch.setattr(repository, "checkpoint_write_guard", interrupted)
    with diagnosis_checkpoint_scope(repository, job_id, "a"), pytest.raises(RuntimeError, match="crash"):
        await write(saver, config, "must rollback")
    assert await raw.aget_tuple(config) is None


async def test_postgres_owner_claim_waits_for_same_transaction(durable_owner):
    repository, sessions, job_id, saver, raw, config = durable_owner
    await expire(sessions, job_id)
    # A fresh claim gives a valid owner, then expiry occurs inside its write.
    await repository.claim_job(job_id, "same-task", "a", execution_token="a")
    async with repository.checkpoint_write_guard(job_id, "a", connection=raw.conn):
        await raw.conn.execute(
            "UPDATE diagnosis_jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE diagnosis_id = %s", (job_id,),
        )
        # Prove another connection cannot acquire this row (NOWAIT fails).
        from sqlalchemy.exc import DBAPIError

        async with sessions.begin() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT diagnosis_id FROM diagnosis_jobs WHERE diagnosis_id=:id FOR UPDATE NOWAIT"), {"id": job_id})
        await raw.aput(config, empty_checkpoint(), {"source": "loop", "step": 1, "parents": {}}, {})
    assert await repository.claim_job(job_id, "same-task", "b", execution_token="b") == "claimed"


async def test_cancelled_transaction_leaves_no_checkpoint(durable_owner, monkeypatch):
    repository, _, job_id, saver, raw, config = durable_owner
    original = repository.checkpoint_write_guard
    inserted = asyncio.Event()

    @asynccontextmanager
    async def pause_commit(*args, **kwargs):
        async with original(*args, **kwargs):
            yield
            inserted.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(repository, "checkpoint_write_guard", pause_commit)
    with diagnosis_checkpoint_scope(repository, job_id, "a"):
        writer = asyncio.create_task(write(saver, config, "cancel before commit"))
    await asyncio.wait_for(inserted.wait(), 3)
    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert await raw.aget_tuple(config) is None


async def test_separate_checkpoint_database_is_rejected(durable_owner):
    repository, _, job_id, _, _, config = durable_owner
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    other = url.rsplit("/", 1)[0] + "/postgres"
    async with AsyncPostgresSaver.from_conn_string(other) as raw:
        await raw.setup()
        saver = FencedCheckpointSaver(raw)
        with diagnosis_checkpoint_scope(repository, job_id, "a"), pytest.raises(RuntimeError, match="same PostgreSQL") as error:
            await write(saver, config, "wrong database")
        assert not isinstance(error.value, DiagnosisLeaseLostError), "configuration errors must fail/retry instead of acknowledging lease_lost"
        assert await raw.aget_tuple(config) is None


async def test_postgres_graph_background_tasks_keep_their_owner_after_takeover(durable_owner):
    repository, sessions, job_id, saver, _, config = durable_owner
    entered, release = asyncio.Event(), asyncio.Event()

    class State(TypedDict):
        value: str

    async def node(state):
        if state["value"] == "old":
            entered.set()
            await release.wait()
        return {"value": state["value"] + " report"}

    workflow = StateGraph(State)
    workflow.add_node("work", node)
    workflow.set_entry_point("work")
    workflow.add_edge("work", END)
    graph = workflow.compile(checkpointer=saver)
    with diagnosis_checkpoint_scope(repository, job_id, "a"):
        old = asyncio.create_task(graph.ainvoke({"value": "old"}, config))
    await asyncio.wait_for(entered.wait(), 3)
    await expire(sessions, job_id)
    await repository.claim_job(job_id, "same-task", "b", execution_token="b")
    try:
        with diagnosis_checkpoint_scope(repository, job_id, "b"):
            assert (await graph.ainvoke({"value": "new"}, config))["value"] == "new report"
    finally:
        release.set()
    with pytest.raises(DiagnosisLeaseLostError):
        await old
    assert (await graph.aget_state(config)).values["value"] == "new report"


async def test_same_database_with_shadow_ownership_table_is_rejected(durable_owner):
    repository, sessions, job_id, saver, raw, config = durable_owner
    schema = "checkpoint_test_" + uuid4().hex
    async with sessions.begin() as session:
        await session.execute(text(f"CREATE SCHEMA {schema}"))
        await session.execute(text(f"CREATE TABLE {schema}.diagnosis_jobs (LIKE public.diagnosis_jobs INCLUDING ALL)"))
        await session.execute(text(f"INSERT INTO {schema}.diagnosis_jobs SELECT * FROM public.diagnosis_jobs WHERE diagnosis_id=:id"), {"id": job_id})
    try:
        await raw.conn.execute(f"SET search_path TO {schema}, public")
        with diagnosis_checkpoint_scope(repository, job_id, "a"), pytest.raises(RuntimeError, match="ownership table"):
            await write(saver, config, "shadow table must not authorize")
        assert await raw.aget_tuple(config) is None
    finally:
        await raw.conn.execute("RESET search_path")
        async with sessions.begin() as session:
            await session.execute(text(f"DROP SCHEMA {schema} CASCADE"))
