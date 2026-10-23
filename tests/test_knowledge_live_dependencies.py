"""Opt-in checks with isolated collection names and real advisory locks."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from langchain_core.documents import Document

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ONCALL_KNOWLEDGE_LIVE_TEST") != "1",
        reason="opt-in local dependencies",
    ),
]


def test_real_postgresql_shared_readers_exclude_writer():
    from app.core.vector_lock import vector_operation_lock

    key = "isolated-lock-test-" + uuid4().hex
    first, second, release, writer = (threading.Event() for _ in range(4))

    def reader(event):
        with vector_operation_lock(key, shared=True):
            event.set()
            assert release.wait(5)

    def write():
        with vector_operation_lock(key):
            writer.set()

    with ThreadPoolExecutor(3) as pool:
        a = pool.submit(reader, first)
        assert first.wait(3)
        b = pool.submit(reader, second)
        assert second.wait(3)
        c = pool.submit(write)
        try:
            assert not writer.wait(0.1)
        finally:
            release.set()
        for task in (a, b, c):
            task.result(5)
        assert writer.is_set()


def test_real_milvus_replacement_uses_shared_version_authority(monkeypatch):
    import psycopg
    from pymilvus import utility

    from app.config import config
    from app.core.milvus_client import MilvusClientManager
    from app.services import vector_store_manager as module

    name = "oncall_fix_six_" + uuid4().hex
    dependency = MilvusClientManager()
    dependency.COLLECTION_NAME = name
    monkeypatch.setattr(module, "milvus_manager", dependency)
    monkeypatch.setattr(
        module.vector_embedding_service,
        "embed_documents",
        lambda texts: [[1.0] * 1024 for _ in texts],
    )
    monkeypatch.setattr(module.vector_embedding_service, "embed_query", lambda _: [1.0] * 1024)
    manager = module.VectorStoreManager()
    manager.collection_name = name
    try:
        dependency.connect()
        manager.replace_documents(
            "source", [Document(page_content="old", metadata={"_file_name": "checkout-api.md"})]
        )
        manager.replace_documents(
            "source", [Document(page_content="new", metadata={"_file_name": "checkout-api.md"})]
        )
        # A different client must read the committed snapshot, not a stale
        # session snapshot which the PostgreSQL version filter would discard.
        reader = module.VectorStoreManager()
        reader.collection_name = name
        assert [doc.page_content for doc in reader.similarity_search("checkout-api", 3)] == ["new"]

        from app.core.vector_lock import vector_operation_lock

        after_commit = []
        with pytest.raises(RuntimeError, match="rollback outer document transaction"):
            with vector_operation_lock("upload:" + name) as coordinator:
                manager.replace_documents(
                    "source",
                    [Document(page_content="uncommitted")],
                    coordinator=coordinator,
                    after_commit=after_commit,
                )
                # The active pointer is still invisible to another connection.
                with psycopg.connect(
                    config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
                ) as observer:
                    active = observer.execute(
                        "SELECT version FROM knowledge_document_versions WHERE collection_name=%s AND source='source'",
                        (name,),
                    ).fetchone()[0]
                staged = dependency.get_collection().query(
                    expr='metadata["_source"] == "source"',
                    output_fields=["metadata", "content"],
                    consistency_level="Strong",
                )
                assert {row["content"] for row in staged} == {"new", "uncommitted"}
                assert (
                    next(row for row in staged if row["content"] == "new")["metadata"][
                        "_index_version"
                    ]
                    == active
                )
                raise RuntimeError("rollback outer document transaction")
        assert [doc.page_content for doc in reader.similarity_search("question", 3)] == ["new"]

        after_commit = []
        with vector_operation_lock("upload:" + name) as coordinator:
            manager.replace_documents(
                "source",
                [Document(page_content="final")],
                coordinator=coordinator,
                after_commit=after_commit,
            )
        for retire in after_commit:
            retire()
        assert [doc.page_content for doc in reader.similarity_search("question", 3)] == ["final"]
    finally:
        # Only the random test collection and its version rows are removed.
        if utility.has_collection(name):
            utility.drop_collection(name)
        with psycopg.connect(
            config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        ) as connection:
            connection.execute(
                "DELETE FROM knowledge_document_versions WHERE collection_name = %s", (name,)
            )
