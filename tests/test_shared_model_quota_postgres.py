"""Explicit opt-in PostgreSQL checks; never infer a business DATABASE_URL."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="explicit TEST_DATABASE_URL required; PostgreSQL integration unverified")


@pytest_asyncio.fixture
async def repositories():
    from app.db.models import Base, ModelQuotaLease, ModelQuotaScope
    from app.repositories.model_quota import ModelQuotaRepository

    url = TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL must name an explicit PostgreSQL test database")
    schema = "quota_test_" + uuid4().hex
    engines = [create_async_engine(url), create_async_engine(url)]
    async with engines[0].begin() as connection:
        await connection.execute(CreateSchema(schema))
        await connection.execution_options(schema_translate_map={None: schema})
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            ModelQuotaScope.__table__, ModelQuotaLease.__table__]))
    sessions = [async_sessionmaker(engine.execution_options(schema_translate_map={None: schema}),
                                  expire_on_commit=False) for engine in engines]
    try:
        yield [ModelQuotaRepository(factory) for factory in sessions], sessions, "synthetic-" + uuid4().hex
    finally:
        async with engines[0].begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_independent_connections_do_not_oversell_and_release_is_idempotent(repositories):
    from app.repositories.model_quota import QuotaLimits, SharedQuotaExceeded

    repos, _sessions, scope = repositories
    limits = QuotaLimits(concurrency=4)
    results = await asyncio.gather(*[repos[index % 2].reserve(scope, 100, limits)
                                    for index in range(20)], return_exceptions=True)
    tokens = [result for result in results if isinstance(result, str)]
    assert len(tokens) == 4 and len(set(tokens)) == 4
    assert all(isinstance(result, str | SharedQuotaExceeded) for result in results)
    await repos[1].release(scope, tokens[0])
    replacement = await repos[0].reserve(scope, 100, limits)
    await repos[1].release(scope, tokens[0])
    with pytest.raises(SharedQuotaExceeded):
        await repos[1].reserve(scope, 100, limits)
    assert await repos[1].renew(scope, replacement, 150)


@pytest.mark.asyncio
async def test_restart_retains_failed_call_charges_and_database_minute_reset(repositories):
    from app.db.models import ModelQuotaScope
    from app.repositories.model_quota import ModelQuotaRepository, QuotaLimits, SharedQuotaExceeded

    repos, sessions, scope = repositories
    limits = QuotaLimits(concurrency=4, calls_per_minute=2, input_bytes_per_minute=10)
    token = await repos[0].reserve(scope, 6, limits)
    await repos[0].release(scope, token)  # Failed provider attempt: no refund.
    restarted = ModelQuotaRepository(sessions[1])
    with pytest.raises(SharedQuotaExceeded):
        await restarted.reserve(scope, 5, limits)
    token = await restarted.reserve(scope, 4, limits)
    await restarted.release(scope, token)
    with pytest.raises(SharedQuotaExceeded):
        await repos[0].reserve(scope, 0, limits)
    async with sessions[0]() as session, session.begin():
        await session.execute(update(ModelQuotaScope).where(ModelQuotaScope.scope == scope).values(
            window_started_at=func.clock_timestamp() - timedelta(minutes=1)))
    await restarted.reserve(scope, 10, limits)


@pytest.mark.asyncio
async def test_database_expiry_stale_release_and_shared_cooldown(repositories):
    from app.db.models import ModelQuotaLease
    from app.repositories.model_quota import QuotaLimits, SharedQuotaExceeded

    repos, sessions, scope = repositories
    limits = QuotaLimits(concurrency=1)
    expired = await repos[0].reserve(scope, 10, limits)
    async with sessions[0]() as session, session.begin():
        await session.execute(update(ModelQuotaLease).where(ModelQuotaLease.call_id == expired).values(
            expires_at=func.clock_timestamp() - timedelta(seconds=1)))
    assert not await repos[1].renew(scope, expired, 150)
    replacement = await repos[1].reserve(scope, 10, limits)
    await repos[0].release(scope, expired)
    with pytest.raises(SharedQuotaExceeded):
        await repos[0].reserve(scope, 10, limits)
    await repos[0].release(scope, replacement)
    await repos[0].cooldown(scope, 30)
    with pytest.raises(SharedQuotaExceeded) as error:
        await repos[1].reserve(scope, 10, limits)
    assert 1 <= error.value.retry_after <= 30
    async with sessions[1]() as session:
        assert (await session.execute(select(func.count()).select_from(ModelQuotaLease).where(
            ModelQuotaLease.scope == scope))).scalar_one() == 0
