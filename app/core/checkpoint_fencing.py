"""Fence every diagnosis checkpoint mutation with its live execution owner."""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import _ainternal
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.repositories.diagnosis import DiagnosisLeaseLostError, DiagnosisRepository


@dataclass(frozen=True)
class CheckpointOwner:
    repository: DiagnosisRepository
    diagnosis_id: str
    execution_token: str


_owner: ContextVar[CheckpointOwner | None] = ContextVar("diagnosis_checkpoint_owner", default=None)


@contextmanager
def diagnosis_checkpoint_scope(repository, diagnosis_id: str, execution_token: str):
    """Context is copied to LangGraph's asynchronous background saver tasks."""
    reset = _owner.set(CheckpointOwner(repository, diagnosis_id, execution_token))
    try:
        yield
    finally:
        _owner.reset(reset)


class FencedCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, inner):
        super().__init__(serde=inner.serde)
        self.inner = inner

    @property
    def config_specs(self):
        return self.inner.config_specs

    def with_allowlist(self, extra_allowlist):
        return FencedCheckpointSaver(self.inner.with_allowlist(extra_allowlist))

    def get_next_version(self, current, channel):
        return self.inner.get_next_version(current, channel)

    def get_tuple(self, config):
        return self.inner.get_tuple(config)

    async def aget_tuple(self, config):
        return await self.inner.aget_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        yield from self.inner.list(config, filter=filter, before=before, limit=limit)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for item in self.inner.alist(config, filter=filter, before=before, limit=limit):
            yield item

    @staticmethod
    def _execution_owner(config):
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        owner = _owner.get()
        if owner is None:
            if thread_id.startswith("aiops:"):
                raise DiagnosisLeaseLostError("diagnosis checkpoint write requires execution ownership")
            return None
        if not owner.execution_token or thread_id != f"aiops:{owner.diagnosis_id}":
            raise DiagnosisLeaseLostError("checkpoint thread does not match the owned diagnosis")
        return owner

    @asynccontextmanager
    async def _write_target(self, config):
        owner = self._execution_owner(config)
        if owner is None:
            yield self.inner
        elif isinstance(self.inner, AsyncPostgresSaver):
            async with self.inner.lock, _ainternal.get_connection(self.inner.conn) as connection:
                async with owner.repository.checkpoint_write_guard(
                    owner.diagnosis_id, owner.execution_token, connection=connection,
                ):
                    yield AsyncPostgresSaver(connection, serde=self.inner.serde)
        else:
            async with owner.repository.checkpoint_write_guard(owner.diagnosis_id, owner.execution_token):
                yield self.inner

    async def aput(self, config, checkpoint, metadata, new_versions):
        async with self._write_target(config) as saver:
            return await saver.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        async with self._write_target(config) as saver:
            await saver.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        async with self._write_target({"configurable": {"thread_id": thread_id}}) as saver:
            await saver.adelete_thread(thread_id)

    def _check_sync_write(self, config):
        if self._execution_owner(config) is not None:
            raise DiagnosisLeaseLostError("diagnosis checkpoint writes must use the guarded async API")

    def put(self, config, checkpoint, metadata, new_versions):
        self._check_sync_write(config)
        return self.inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        self._check_sync_write(config)
        return self.inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id):
        self._check_sync_write({"configurable": {"thread_id": thread_id}})
        return self.inner.delete_thread(thread_id)

    # Bulk copy/prune/delete-for-runs are intentionally not delegated. The base
    # class rejects them, so they cannot bypass a diagnosis ownership guard.
