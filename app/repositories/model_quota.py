"""PostgreSQL quota: all mutations serialize on the provider-account scope row."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import ModelQuotaLease, ModelQuotaScope


class SharedQuotaExceeded(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = max(1, ceil(retry_after))
        super().__init__("共享模型额度已耗尽，请稍后重试")


@dataclass(frozen=True)
class QuotaLimits:
    concurrency: int = 4
    calls_per_minute: int = 60
    input_bytes_per_minute: int = 1048576
    lease_seconds: float = 150.0


def reserve_window(state, expirations, now, limits: QuotaLimits, input_bytes: int):
    """Apply one admission using only database-sourced time and locked state."""
    if isinstance(input_bytes, bool) or not isinstance(input_bytes, int) or input_bytes < 0:
        raise ValueError("input_bytes must be a nonnegative integer")
    if state.cooldown_until is not None and state.cooldown_until > now:
        raise SharedQuotaExceeded((state.cooldown_until - now).total_seconds())
    active = [expiry for expiry in expirations if expiry > now]
    if len(active) >= limits.concurrency:
        raise SharedQuotaExceeded((min(active) - now).total_seconds())
    minute = now.replace(second=0, microsecond=0)
    if state.window_started_at != minute:
        state.window_started_at = minute
        state.calls = state.input_bytes = 0
    if state.calls >= limits.calls_per_minute or state.input_bytes + input_bytes > limits.input_bytes_per_minute:
        raise SharedQuotaExceeded((minute + timedelta(minutes=1) - now).total_seconds())
    state.calls += 1
    state.input_bytes += input_bytes


class ModelQuotaRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @asynccontextmanager
    async def _locked(self, scope):
        async with self.session_factory() as session, session.begin():
            # A concurrent first insert waits on the unique key, then locks the
            # committed row. Never use process clocks or transaction-start now().
            await session.execute(insert(ModelQuotaScope).values(
                scope=scope, window_started_at=func.clock_timestamp(), calls=0, input_bytes=0
            ).on_conflict_do_nothing(index_elements=[ModelQuotaScope.scope]))
            state = (await session.execute(select(ModelQuotaScope).where(
                ModelQuotaScope.scope == scope
            ).with_for_update())).scalar_one()
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            yield session, state, now

    async def reserve(self, scope, input_bytes, limits: QuotaLimits, *, call_id=None) -> str:
        call_id = call_id or str(uuid4())
        async with self._locked(scope) as (session, state, now):
            await session.execute(delete(ModelQuotaLease).where(
                ModelQuotaLease.scope == scope, ModelQuotaLease.expires_at <= now))
            expirations = (await session.execute(select(ModelQuotaLease.expires_at).where(
                ModelQuotaLease.scope == scope))).scalars().all()
            reserve_window(state, expirations, now, limits, input_bytes)
            session.add(ModelQuotaLease(call_id=call_id, scope=scope,
                                       expires_at=now + timedelta(seconds=limits.lease_seconds)))
        return call_id

    async def renew(self, scope, call_id, lease_seconds) -> bool:
        async with self._locked(scope) as (session, _state, now):
            lease = await session.get(ModelQuotaLease, call_id)
            if lease is None or lease.scope != scope or lease.expires_at <= now:
                return False
            lease.expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def release(self, scope, call_id):
        async with self._locked(scope) as (session, _state, _now):
            await session.execute(delete(ModelQuotaLease).where(
                ModelQuotaLease.scope == scope, ModelQuotaLease.call_id == call_id))
            # Minute reservations intentionally survive completion/failure.

    async def cooldown(self, scope, seconds):
        async with self._locked(scope) as (_session, state, now):
            until = now + timedelta(seconds=seconds)
            state.cooldown_until = max(state.cooldown_until or until, until)
