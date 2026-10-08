"""Async PostgreSQL engine lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import config


class DatabaseManager:
    def __init__(self) -> None:
        self.engine: AsyncEngine = create_async_engine(
            config.database_url,
            pool_pre_ping=True,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Wait for PostgreSQL and optionally create the local-development schema."""
        last_error: Exception | None = None
        connected = False
        for attempt in range(1, config.database_connect_retries + 1):
            try:
                async with self.engine.begin() as connection:
                    await connection.execute(text("SELECT 1"))
                connected = True
                break
            except Exception as exc:  # pragma: no cover - depends on external startup timing
                last_error = exc
                if attempt >= config.database_connect_retries:
                    break
                logger.warning(
                    "PostgreSQL 尚未就绪（{}/{}）：{}",
                    attempt,
                    config.database_connect_retries,
                    exc,
                )
                await asyncio.sleep(config.database_connect_retry_seconds)
        if not connected:
            raise RuntimeError(f"PostgreSQL 连接失败: {last_error}") from last_error

        if config.database_auto_create:
            await asyncio.to_thread(self._upgrade_schema)
        logger.info("PostgreSQL 连接成功，诊断持久化已就绪")

    @staticmethod
    def _upgrade_schema() -> None:
        project_root = Path(__file__).resolve().parents[2]
        alembic_config = AlembicConfig(str(project_root / "alembic.ini"))
        command.upgrade(alembic_config, "head")

    async def health_check(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("PostgreSQL 健康检查失败: {}", exc)
            return False

    async def close(self) -> None:
        await self.engine.dispose()


database_manager = DatabaseManager()
