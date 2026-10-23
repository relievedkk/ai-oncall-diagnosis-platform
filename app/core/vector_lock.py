"""Coordinate Milvus operations across processes through PostgreSQL."""

import hashlib
from contextlib import contextmanager, nullcontext

import psycopg

from app.config import config


@contextmanager
def vector_operation_lock(collection_name: str, *, shared: bool = False, connection=None):
    # Every deployment sharing this collection must use the same coordinator DB.
    identity = f"oncall:milvus:{collection_name}".encode()
    key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
    url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    # A caller-owned transaction must keep its document lock until the version
    # pointer commits. Entering an existing psycopg connection would commit early.
    transaction = (
        nullcontext(connection)
        if connection is not None
        else psycopg.connect(url, connect_timeout=10)
    )
    with transaction as connection:
        connection.execute("SET LOCAL lock_timeout = '30s'")
        function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        connection.execute(f"SELECT {function}(%s)", (key,))
        yield connection
