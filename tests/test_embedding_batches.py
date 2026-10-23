"""Large documents must respect provider batches without losing vector alignment."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.vector_embedding_service import DashScopeEmbeddings


class ProviderBoundary:
    def __init__(self, *, fail_on=None, missing=False):
        self.calls = []
        self.fail_on = fail_on
        self.missing = missing

    def create(self, *, model, input, dimensions, encoding_format):
        self.calls.append(list(input))
        if len(input) > 10:
            raise ValueError("provider batch limit is 10")
        if len(self.calls) == self.fail_on:
            raise OSError("temporary provider failure")
        data = [
            SimpleNamespace(index=index, embedding=[float(text.removeprefix("text-"))])
            for index, text in enumerate(input)
        ]
        if self.missing:
            data = data[:-1]
        return SimpleNamespace(data=list(reversed(data)))


def embeddings(provider):
    with patch(
        "app.services.vector_embedding_service.OpenAI",
        return_value=SimpleNamespace(embeddings=provider),
    ):
        return DashScopeEmbeddings(api_key="unit-test-only", dimensions=1)


def test_large_document_embeds_every_chunk_in_input_order():
    provider = ProviderBoundary()
    result = embeddings(provider).embed_documents([f"text-{i}" for i in range(41)])
    assert result == [[float(i)] for i in range(41)]
    assert [len(batch) for batch in provider.calls] == [10, 10, 10, 10, 1]


def test_provider_response_indexes_determine_document_alignment():
    result = embeddings(ProviderBoundary()).embed_documents(["text-1", "text-2"])
    assert result == [[1.0], [2.0]]


def test_empty_input_does_not_call_provider():
    provider = ProviderBoundary()
    assert embeddings(provider).embed_documents([]) == []
    assert provider.calls == []


def test_later_batch_failure_does_not_return_partial_embeddings():
    provider = ProviderBoundary(fail_on=2)
    with pytest.raises(RuntimeError, match="temporary provider failure"):
        embeddings(provider).embed_documents([f"text-{i}" for i in range(11)])
    assert [len(batch) for batch in provider.calls] == [10, 1]


def test_incomplete_provider_response_is_rejected():
    with pytest.raises(RuntimeError, match="incomplete"):
        embeddings(ProviderBoundary(missing=True)).embed_documents(["text-1", "text-2"])
