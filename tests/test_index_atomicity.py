"""Replacement failures must never destroy the previous searchable document."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

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


@pytest.fixture
def indexed_file(tmp_path, monkeypatch):
    path = tmp_path / "runbook.txt"
    path.write_text("replacement", encoding="utf-8")
    collection = MemoryCollection(path.as_posix())
    manager = VectorStoreManager()
    manager.vector_store = MemoryStore(collection)
    monkeypatch.setattr(index_module, "vector_store_manager", manager)
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
