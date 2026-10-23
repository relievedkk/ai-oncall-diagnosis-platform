from contextlib import nullcontext

import pytest
from langchain_core.documents import Document

from app.services.vector_store_manager import VectorStoreManager


@pytest.fixture(autouse=True)
def offline_embedding(monkeypatch):
    monkeypatch.setattr("app.services.vector_store_manager.vector_embedding_service.embed_query", lambda _: [1.0] * 1024)


def test_explicit_service_prefers_matching_sources(monkeypatch):
    calls = []
    target = Document(page_content="target service", metadata={"_file_name": "report-api.md"})

    class Store:
        def similarity_search_by_vector(self, query, k, **kwargs):
            calls.append(kwargs)
            return [target] if kwargs.get("expr") else []

    manager = VectorStoreManager()
    monkeypatch.setattr(manager, "_read_guard", nullcontext)
    monkeypatch.setattr(manager, "_initialize_vector_store", lambda: Store())
    assert manager.similarity_search("report-api 的恢复验证码是什么？", 5) == [target]
    assert "report-api" in calls[0]["expr"]


def test_missing_service_filename_falls_back_to_semantic_search(monkeypatch):
    target = Document(page_content="arbitrary filename")

    class Store:
        def similarity_search_by_vector(self, query, k, **kwargs):
            return [] if kwargs.get("expr") else [target]

    manager = VectorStoreManager()
    monkeypatch.setattr(manager, "_read_guard", nullcontext)
    monkeypatch.setattr(manager, "_initialize_vector_store", lambda: Store())
    assert manager.similarity_search("report-api 的恢复码？", 5) == [target]


def test_service_substring_does_not_select_another_service(monkeypatch):
    wrong = Document(page_content="wrong", metadata={"_file_name": "oauth-api.md"})
    correct = Document(page_content="correct", metadata={"_file_name": "auth-api.md"})

    class Store:
        def similarity_search_by_vector(self, query, k, **kwargs):
            return [wrong] if kwargs.get("expr") else [correct]

    manager = VectorStoreManager()
    monkeypatch.setattr(manager, "_read_guard", nullcontext)
    monkeypatch.setattr(manager, "_initialize_vector_store", lambda: Store())
    assert manager.similarity_search("auth-api 的恢复码？", 5) == [correct]
