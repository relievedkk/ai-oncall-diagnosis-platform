"""Coordinate Milvus operations across processes through PostgreSQL."""

import hashlib
from contextlib import contextmanager

import psycopg

from app.config import config


@contextmanager
def vector_operation_lock(collection_name: str):
    # Every deployment sharing this collection must use the same coordinator DB.
    identity = f"oncall:milvus:{collection_name}".encode()
    key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
    url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    with psycopg.connect(url, connect_timeout=10) as connection:
        connection.execute("SET LOCAL lock_timeout = '30s'")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        yield connection
