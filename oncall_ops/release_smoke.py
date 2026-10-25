"""Project-interpreter schema read, with no migrations or external model calls."""

import argparse
import asyncio
import json
import sys


async def database_schema():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import config

    if not config.database_url.startswith('postgresql+'):
        raise ValueError('Release schema verification requires PostgreSQL')
    engine = create_async_engine(config.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalars().all()
            if len(rows) != 1 or not rows[0]:
                raise ValueError('Expected exactly one applied Alembic revision')
            return rows[0]
    finally:
        await engine.dispose()


async def rollback_safety(*, engine_factory=None, database_url=None):
    """Prove rollback safety using a database-enforced read-only session."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import config

    url = database_url or config.database_url
    if not url.startswith('postgresql+'):
        raise ValueError('Release rollback verification requires PostgreSQL')
    factory = engine_factory or create_async_engine
    engine = factory(url, pool_pre_ping=True, connect_args={
        'server_settings': {'default_transaction_read_only': 'on'}})
    try:
        async with engine.connect() as connection:
            read_only = (await connection.execute(text('SHOW transaction_read_only'))).scalar_one()
            if str(read_only).lower() != 'on':
                raise ValueError('Rollback proof connection is not read-only')
            schemas = (await connection.execute(
                text('SELECT version_num FROM alembic_version'))).scalars().all()
            if len(schemas) != 1 or not schemas[0]:
                raise ValueError('Expected exactly one applied Alembic revision')
            durable = (await connection.execute(text("""
                SELECT
                  (SELECT count(*) FROM diagnosis_jobs
                   WHERE status IN ('queued','running','retrying')) +
                  (SELECT count(*) FROM remediation_actions
                   WHERE status IN ('approved','executing','reconciling','verifying','rolling_back'))
            """))).scalar_one()
            return {'schema': schemas[0], 'durable_work': int(durable)}
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--schema', action='store_true')
    mode.add_argument('--rollback-safe', action='store_true')
    args = parser.parse_args()
    try:
        value = asyncio.run(asyncio.wait_for(
            rollback_safety() if args.rollback_safe else database_schema(), timeout=20))
    except Exception:
        print('Schema verification failed; check selected configuration and PostgreSQL', file=sys.stderr)
        return 1
    print(json.dumps(value if isinstance(value, dict) else {'schema': value}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
