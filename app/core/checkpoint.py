"""Lifecycle-managed durable LangGraph checkpoint storage."""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from loguru import logger
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import config


class CheckpointManager:
    """Own one process-local checkpointer backed by the shared PostgreSQL database."""

    def __init__(self) -> None:
        self._checkpointer: Any | None = None
        self._pool: AsyncConnectionPool | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer

        async with self._lock:
            if self._checkpointer is not None:
                return self._checkpointer

            if not config.langgraph_checkpoint_enabled:
                self._checkpointer = MemorySaver()
                logger.warning("LangGraph durable checkpoints are disabled")
                return self._checkpointer

            pool = AsyncConnectionPool(
                conninfo=config.checkpoint_database_url,
                min_size=1,
                max_size=config.database_pool_size,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            try:
                await pool.open(wait=True)
                checkpointer = AsyncPostgresSaver(pool)
                await checkpointer.setup()
            except Exception:
                await pool.close()
                raise

            self._pool = pool
            self._checkpointer = checkpointer
            logger.info("LangGraph PostgreSQL checkpoints are ready")
            return checkpointer

    async def close(self) -> None:
        async with self._lock:
            pool = self._pool
            self._pool = None
            self._checkpointer = None
            if pool is not None:
                await pool.close()
                logger.info("LangGraph checkpoint connection closed")


checkpoint_manager = CheckpointManager()
