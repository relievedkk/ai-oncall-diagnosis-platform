import pytest

from oncall_ops.release_smoke import rollback_safety


@pytest.mark.asyncio
async def test_rollback_safety_uses_read_only_connection_and_direct_counts():
    statements = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass

        async def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if 'transaction_read_only' in sql:
                return Result('on')
            if 'alembic_version' in sql:
                return Result(['schema-1'])
            return Result(0)

    class Engine:
        def connect(self): return Connection()
        async def dispose(self): pass

    created = []

    def factory(url, **options):
        created.append((url, options))
        return Engine()

    assert await rollback_safety(
        engine_factory=factory,
        database_url='postgresql+asyncpg://oncall:test@127.0.0.1/oncall',
    ) == {'schema': 'schema-1', 'durable_work': 0}
    assert created[0][1]['connect_args']['server_settings']['default_transaction_read_only'] == 'on'
    assert any('diagnosis_jobs' in sql and 'remediation_actions' in sql for sql in statements)
