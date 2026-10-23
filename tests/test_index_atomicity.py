"""Replacement failures must never destroy the previous searchable document."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from langchain_core.documents import Document

from app.services import vector_index_service as index_module
from app.services.vector_store_manager import VectorStoreManager


class MemoryCollection:
    """Milvus query/delete/flush semantics with explicit primary keys."""

    def __init__(self, source):
        self.rows = {"old": Document(page_content="old", metadata={"_source": source})}
        self.events = []

    def query(self, expr, output_fields, **kwargs):
        source = json.loads(expr.split(" == ", 1)[1])
        return [{"id": key} for key, doc in self.rows.items() if doc.metadata["_source"] == source]

    def delete(self, expr):
        self.events.append("delete")
        if expr.startswith("id in "):
            keys = json.loads(expr[len("id in ") :])
        else:
            keys = [row["id"] for row in self.query(expr, ["id"])]
        for key in keys:
            self.rows.pop(key, None)

    def flush(self):
        self.events.append("flush")


class MemoryStore:
    def __init__(self, collection, failure=None):
        self.collection = collection
        self.failure = failure

    def add_documents(self, documents, ids):
        self.collection.events.append("insert")
        if self.failure == "embedding":
            raise RuntimeError("embedding unavailable")
        for index, (doc, key) in enumerate(zip(documents, ids, strict=True)):
            self.collection.rows[key] = doc
            if self.failure == "insert" and index == 0:
                raise RuntimeError("partial insert")
        return ids

    def add_embeddings(self, texts, embeddings, metadatas, ids, **kwargs):
        return self.add_documents(
            [
                Document(page_content=text, metadata=metadata)
                for text, metadata in zip(texts, metadatas, strict=True)
            ],
            ids,
        )


@pytest.fixture
def indexed_file(tmp_path, monkeypatch):
    path = tmp_path / "runbook.txt"
    path.write_text("replacement", encoding="utf-8")
    collection = MemoryCollection(path.as_posix())
    manager = VectorStoreManager()
    manager.vector_store = MemoryStore(collection)
    monkeypatch.setattr(manager, "_distributed_lock", lambda: nullcontext())
    monkeypatch.setattr(index_module, "vector_store_manager", manager)
    monkeypatch.setattr(
        "app.services.vector_store_manager.vector_embedding_service.embed_documents",
        lambda texts: [[1.0] * 1024 for _ in texts],
    )
    monkeypatch.setattr(
        "app.services.vector_store_manager.milvus_manager.get_collection", lambda: collection
    )
    return path, manager, collection


@pytest.mark.parametrize("failure", ["parse", "empty", "embedding", "insert"])
def test_failed_reindex_keeps_previous_vectors(indexed_file, monkeypatch, failure):
    path, manager, collection = indexed_file
    manager.vector_store.failure = failure

    def split(source):
        if failure == "parse":
            raise ValueError("invalid file")
        if failure == "empty":
            return []
        return [Document(page_content="new", metadata={"_source": source})]

    monkeypatch.setattr(index_module.document_splitter_service, "split_document", split)
    with pytest.raises(RuntimeError):
        index_module.VectorIndexService().index_single_file(str(path))
    assert list(collection.rows) == ["old"]
    assert collection.rows["old"].page_content == "old"


def test_successful_reindex_persists_new_version_before_removing_old(indexed_file):
    path, _, collection = indexed_file
    index_module.VectorIndexService().index_single_file(str(path))
    assert [doc.page_content for doc in collection.rows.values()] == ["replacement"]
    assert collection.events.index("flush") < collection.events.index("delete")
    assert all(doc.metadata.get("_index_version") for doc in collection.rows.values())


def test_retirement_flush_failure_keeps_committed_new_version(indexed_file):
    path, manager, collection = indexed_file
    flushes = 0

    def flush():
        nonlocal flushes
        flushes += 1
        if flushes == 2:
            raise RuntimeError("retirement flush failed")

    collection.flush = flush
    result = manager.replace_documents(path.as_posix(), [Document(page_content="new")])
    assert result
    assert any(doc.page_content == "new" for doc in collection.rows.values())


def test_uncertain_coordinator_commit_preserves_both_versions(indexed_file, monkeypatch):
    from contextlib import contextmanager

    from app.services.vector_store_manager import IndexCommitUncertain

    path, manager, collection = indexed_file

    @contextmanager
    def lost_commit_response():
        yield None
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(manager, "_distributed_lock", lost_commit_response)
    with pytest.raises(IndexCommitUncertain):
        manager.replace_documents(path.as_posix(), [Document(page_content="new")])
    assert {doc.page_content for doc in collection.rows.values()} == {"old", "new"}


def test_concurrent_replacements_keep_only_one_complete_version(indexed_file):
    path, manager, collection = indexed_file
    first_inserting = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_inserting = threading.Event()
    original_add = manager.vector_store.add_documents

    def blocking_add(documents, ids):
        if documents[0].page_content == "version-a":
            first_inserting.set()
            assert release_first.wait(timeout=5)
        else:
            second_inserting.set()
        return original_add(documents, ids)

    manager.vector_store.add_documents = blocking_add

    def replace(text):
        if text == "version-b":
            second_started.set()
        manager.replace_documents(path.as_posix(), [Document(page_content=text)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(replace, "version-a")
        assert first_inserting.wait(timeout=5)
        second = executor.submit(replace, "version-b")
        try:
            assert second_started.wait(timeout=5)
            assert not second_inserting.wait(timeout=0.1)
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert len(collection.rows) == 1
    assert next(iter(collection.rows.values())).page_content in {"version-a", "version-b"}


@pytest.mark.integration
def test_separate_processes_serialize_replacements(monkeypatch):
    import multiprocessing
    import os

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL required")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as shared:
        rows = shared.dict(
            {"old": Document(page_content="old", metadata={"_source": "shared.txt"})}
        )
        first_inside, release, second_started, second_inside = (ctx.Event() for _ in range(4))
        results = ctx.Queue()
        a = ctx.Process(
            target=_replace_in_process, args=(rows, "a", first_inside, release, results, None)
        )
        b = ctx.Process(
            target=_replace_in_process,
            args=(rows, "b", second_inside, None, results, second_started),
        )
        a.start()
        try:
            assert first_inside.wait(20)
            b.start()
            assert second_started.wait(20)
            assert not second_inside.wait(1)
        finally:
            release.set()
            a.join(20)
            if b.pid is not None:
                b.join(20)
            for process in (a, b):
                if process.pid is not None and process.is_alive():
                    process.terminate()
                    process.join()
        assert a.exitcode == b.exitcode == 0
        assert [results.get(timeout=3) for _ in range(2)] == ["ok", "ok"]
        assert len(rows) == 1
        assert next(iter(rows.values())).page_content == "b"


def _replace_in_process(rows, text, inside, release, results, started):
    from app.services import vector_store_manager as module

    collection = MemoryCollection("shared.txt")
    module.vector_embedding_service.embed_documents = lambda texts: [[1.0] * 1024 for _ in texts]
    collection.rows = rows
    store = MemoryStore(collection)
    original = store.add_documents

    def add(documents, ids):
        inside.set()
        if release is not None:
            assert release.wait(20)
        return original(documents, ids)

    store.add_documents = add
    manager = VectorStoreManager()
    manager.vector_store = store
    module.milvus_manager.get_collection = lambda: collection
    if started is not None:
        started.set()
    try:
        manager.replace_documents("shared.txt", [Document(page_content=text)])
        results.put("ok")
    except Exception as exc:
        results.put(type(exc).__name__)
        raise


def test_coordinator_failure_never_touches_existing_vectors(indexed_file, monkeypatch):
    from contextlib import contextmanager

    path, manager, collection = indexed_file

    @contextmanager
    def unavailable():
        raise RuntimeError("coordinator unavailable")
        yield

    monkeypatch.setattr(manager, "_distributed_lock", unavailable)
    with pytest.raises(RuntimeError, match="coordinator unavailable"):
        manager.replace_documents(path.as_posix(), [Document(page_content="new")])
    assert list(collection.rows) == ["old"]
    assert collection.events == []


def test_external_transaction_defers_retirement_until_owner_commits(indexed_file, monkeypatch):
    from contextlib import contextmanager

    path, manager, collection = indexed_file
    owner = object()
    acquired = []

    @contextmanager
    def guard(connection=None):
        acquired.append(connection)
        yield None

    monkeypatch.setattr(manager, "_operation_guard", guard)
    after_commit = []
    manager.replace_documents(
        path.as_posix(),
        [Document(page_content="new")],
        coordinator=owner,
        after_commit=after_commit,
    )
    assert acquired == [owner]
    assert {doc.page_content for doc in collection.rows.values()} == {"old", "new"}
    assert len(after_commit) == 1
    after_commit[0]()
    assert [doc.page_content for doc in collection.rows.values()] == ["new"]
