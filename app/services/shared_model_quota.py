"""Shared call lifecycle; permits cover provider I/O, including streamed output.

The bound is on effective database leases. Provider work may outlive network
cancellation; renewal loss poisons this process guard until a managed restart.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import uuid4

from app.repositories.model_quota import QuotaLimits, SharedQuotaExceeded

__all__ = ["SharedModelQuota", "SharedQuotaExceeded", "SharedQuotaUnavailable", "get_shared_model_quota"]


class SharedQuotaUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("共享模型额度服务不可用，已停止模型调用")


def is_provider_429(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "status_code", None) == 429:
            return True
        error = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
    return False


class SharedModelQuota:
    def __init__(self, repository, *, scope, limits=None, renew_seconds=30.0,
                 cooldown_seconds=30.0, operation_timeout=5.0):
        self.repository = repository
        self.scope = scope
        self.limits = limits or QuotaLimits()
        self.renew_seconds = renew_seconds
        self.cooldown_seconds = cooldown_seconds
        self.operation_timeout = operation_timeout
        self.broken = False
        self._cleanup_tasks = set()

    async def _db(self, operation):
        return await asyncio.wait_for(operation, timeout=self.operation_timeout)

    async def _release(self, call_id):
        try:
            await self._db(self.repository.release(self.scope, call_id))
        except Exception:
            # The expired lease is the recovery path; never silently reopen.
            self.broken = True

    async def _cleanup(self, call_id):
        task = asyncio.create_task(self._release(call_id))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    @asynccontextmanager
    async def hold(self, input_bytes):
        if self.broken:
            raise SharedQuotaUnavailable()
        call_id = str(uuid4())
        try:
            await self._db(self.repository.reserve(self.scope, input_bytes, self.limits, call_id=call_id))
        except SharedQuotaExceeded:
            raise
        except asyncio.CancelledError:
            await self._cleanup(call_id)
            raise
        except Exception:
            self.broken = True
            await self._cleanup(call_id)
            raise SharedQuotaUnavailable() from None

        if self.broken:
            await self._cleanup(call_id)
            raise SharedQuotaUnavailable()

        owner = asyncio.current_task()
        lost = False

        async def renew():
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(self.renew_seconds)
                    if not await self._db(self.repository.renew(self.scope, call_id, self.limits.lease_seconds)):
                        raise SharedQuotaUnavailable()
            except asyncio.CancelledError:
                raise
            except Exception:
                lost = self.broken = True
                owner.cancel()

        renewal = asyncio.create_task(renew())
        try:
            yield call_id
            if lost:
                raise SharedQuotaUnavailable()
        except asyncio.CancelledError:
            if lost:
                # Remove only our cancellation request, preserving external ones.
                owner.uncancel()
                raise SharedQuotaUnavailable() from None
            raise
        except Exception as error:
            if is_provider_429(error):
                try:
                    await self._db(self.repository.cooldown(self.scope, self.cooldown_seconds))
                except Exception:
                    self.broken = True
                    raise SharedQuotaUnavailable() from None
            raise
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            await self._cleanup(call_id)


@lru_cache(maxsize=1)
def get_shared_model_quota():
    # Lazy construction keeps disabled/offline paths independent of PostgreSQL.
    from app.config import config
    from app.db.session import database_manager
    from app.repositories.model_quota import ModelQuotaRepository

    return SharedModelQuota(ModelQuotaRepository(database_manager.session_factory),
        scope=config.shared_model_quota_scope,
        limits=QuotaLimits(config.shared_model_quota_concurrency,
                           config.shared_model_quota_calls_per_minute,
                           config.shared_model_quota_input_bytes_per_minute,
                           config.shared_model_quota_lease_seconds),
        renew_seconds=config.shared_model_quota_renew_seconds,
        cooldown_seconds=config.provider_cooldown_seconds)


_transport_owner = ContextVar("shared_model_transport_owner", default=None)


@dataclass
class _Transport:
    identity: tuple
    streams: list = field(default_factory=list)
    active: bool = True


def track_provider_stream(iterator):
    owner = _transport_owner.get()
    if owner is not None and owner.active:
        owner.streams.append(iterator)


@asynccontextmanager
async def shared_transport(model, input_bytes):
    """Avoid nested generate→stream reservations without bypassing sibling tasks."""
    identity = (id(model), asyncio.current_task())
    previous = _transport_owner.get()
    if previous is not None and previous.active and previous.identity == identity:
        yield
        return
    owner = _Transport(identity)
    token = _transport_owner.set(owner)
    try:
        async with get_shared_model_quota().hold(input_bytes):
            from app.config import config

            try:
                async with asyncio.timeout(config.llm_request_timeout):
                    yield
            finally:
                # LangChain's public astream does not close a suspended inner
                # generator on early aclose. Close real provider streams here.
                for iterator in reversed(owner.streams):
                    await asyncio.wait_for(iterator.aclose(), timeout=5.0)
    finally:
        owner.active = False
        try:
            _transport_owner.reset(token)
        except ValueError:
            # Async generator GC may run in a fresh context. Inactive owner
            # objects cannot bypass a future admission in the original one.
            _transport_owner.set(None)
