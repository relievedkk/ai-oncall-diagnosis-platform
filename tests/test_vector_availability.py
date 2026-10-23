import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from langchain_core.documents import Document
from langchain_core.tools import ToolException

from app.services import vector_store_manager as module


@pytest.fixture
def manager(monkeypatch):
    manager = module.VectorStoreManager()
    monkeypatch.setattr(manager, "_distributed_lock", lambda: nullcontext())
    monkeypatch.setattr(manager, "_distributed_read_lock", lambda: nullcontext(), raising=False)
    monkeypatch.setattr(module.vector_embedding_service, "embed_query", lambda _: [1.0] * 1024)
    return manager


def test_independent_readers_do_not_serialize(manager):
    both_reading = threading.Barrier(2)
    expected = Document(page_content="current document")

    class Store:
        def similarity_search(self, *args, **kwargs):
            both_reading.wait(timeout=2)
            return [expected]

        similarity_search_by_vector = similarity_search

    manager.vector_store = Store()
    with ThreadPoolExecutor(2) as pool:
        requests = [pool.submit(manager.similarity_search, "question") for _ in range(2)]
        assert [request.result(timeout=3) for request in requests] == [[expected], [expected]]


def test_embedding_work_does_not_hold_collection_write_lock(manager, monkeypatch):
    from test_index_atomicity import MemoryCollection

    collection = MemoryCollection("source")
    preparing, release = threading.Event(), threading.Event()
    expected = Document(page_content="old searchable document")

    def embed(texts):
        preparing.set()
        assert release.wait(3)
        return [[1.0] * 1024 for _ in texts]

    class Store:
        def add_documents(self, docs, ids):
            module.vector_embedding_service.embed_documents([d.page_content for d in docs])
            return ids

        def add_embeddings(self, texts, embeddings, metadatas, ids, **kwargs):
            return ids

        def similarity_search(self, *args, **kwargs):
            return [expected]

        similarity_search_by_vector = similarity_search

    manager.vector_store = Store()
    monkeypatch.setattr(module.vector_embedding_service, "embed_documents", embed)
    monkeypatch.setattr(module.milvus_manager, "get_collection", lambda: collection)
    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(manager.replace_documents, "source", [Document(page_content="new")])
        assert preparing.wait(2)
        read = pool.submit(manager.similarity_search, "question")
        try:
            assert read.result(timeout=0.4) == [expected]
        finally:
            release.set()
            write.result(timeout=3)


def test_provider_failure_is_not_a_successful_empty_search(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private provider detail")

    class Store:
        similarity_search = fail
        similarity_search_by_vector = fail

    manager.vector_store = Store()
    with pytest.raises(RuntimeError, match="知识检索"):
        manager.similarity_search("question")


def test_knowledge_tool_reports_failure_instead_of_absent_documents(monkeypatch):
    from app.tools import knowledge_tool

    def fail(*args, **kwargs):
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(knowledge_tool.vector_store_manager, "similarity_search", fail)
    with pytest.raises(ToolException, match="暂不可用") as error:
        knowledge_tool.retrieve_knowledge.invoke({"query": "question"})
    assert "private provider detail" not in str(error.value)


def test_legacy_uploaded_vectors_are_quarantined_from_retrieval(manager):
    legacy = Document(
        page_content="obsolete",
        metadata={"_file_name": "a" * 32 + ".md", "_source": "uploads/" + "a" * 32 + ".md"},
    )
    current = Document(page_content="current", metadata={"_file_name": "checkout-api.md"})

    class Store:
        def similarity_search_by_vector(self, *args, **kwargs):
            return [legacy, current]

    manager.vector_store = Store()
    assert manager.similarity_search("question", k=3) == [current]


def test_only_the_shared_committed_version_is_returned(manager, monkeypatch):
    old = Document(page_content="obsolete", metadata={"_source": "source", "_index_version": "old"})
    current = Document(
        page_content="current", metadata={"_source": "source", "_index_version": "new"}
    )

    class Coordinator:
        def execute(self, *args, **kwargs):
            return self

        def fetchall(self):
            return [("source", "new")]

    monkeypatch.setattr(manager, "_distributed_read_lock", lambda: nullcontext(Coordinator()))

    class Store:
        def similarity_search_by_vector(self, *args, **kwargs):
            return [old, current]

    manager.vector_store = Store()
    assert manager.similarity_search("question", k=3) == [current]


def test_uncommitted_directory_chunks_are_not_served(manager, monkeypatch):
    orphan = Document(
        page_content="uncommitted",
        metadata={
            "_source": "/app/aiops-docs/runbook.md",
            "_index_version": "pending",
            "_version_coordinated": True,
        },
    )

    class Coordinator:
        def execute(self, *args, **kwargs):
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(manager, "_distributed_read_lock", lambda: nullcontext(Coordinator()))

    class Store:
        def similarity_search_by_vector(self, *args, **kwargs):
            return [orphan]

    manager.vector_store = Store()
    assert manager.similarity_search("question", k=3) == []


def test_search_reads_fresh_milvus_snapshot_after_shared_commit(manager):
    current = Document(page_content="just committed")

    class Store:
        def similarity_search_by_vector(self, *args, **kwargs):
            return [current] if kwargs.get("consistency_level") == "Strong" else []

    manager.vector_store = Store()
    assert manager.similarity_search("question") == [current]
