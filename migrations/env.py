"""Alembic environment for the async PostgreSQL connection."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import config as app_config
from app.db.models import Base

alembic_config = context.config
alembic_config.set_main_option("sqlalchemy.url", app_config.database_url)
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata
EXTERNALLY_MANAGED_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
}


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Exclude tables owned and migrated by langgraph-checkpoint-postgres."""
    table_name = name if type_ == "table" else getattr(getattr(object_, "table", None), "name", None)
    return table_name not in EXTERNALLY_MANAGED_TABLES


def run_migrations_offline() -> None:
    context.configure(
        url=alembic_config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
