import io
import json
from contextlib import nullcontext

import pytest
from fastapi import HTTPException, UploadFile

from app.api import file as file_api
from app.services import vector_index_service as index_module


@pytest.fixture
def documents(tmp_path, monkeypatch):
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        index_module, "upload_document_lock", lambda _: nullcontext(), raising=False
    )
    current = {}

    class Index:
        def replace_documents(self, source, docs, **kwargs):
            if any("FAIL" in doc.page_content for doc in docs):
                raise RuntimeError("simulated indexing failure")
            current[source] = docs

    monkeypatch.setattr(index_module, "vector_store_manager", Index())
    return tmp_path, current


async def upload(name, text):
    return await file_api.upload_file(UploadFile(filename=name, file=io.BytesIO(text.encode())))


@pytest.mark.asyncio
async def test_same_named_update_replaces_old_vectors_and_preserves_original_source(documents):
    _, current = documents
    await upload("checkout-api.md", "# checkout-api\nold instructions")
    await upload("checkout-api.md", "# checkout-api\nnew instructions")
    assert len(current) == 1
    docs = next(iter(current.values()))
    assert "new instructions" in docs[0].page_content
    assert docs[0].metadata["_file_name"] == "checkout-api.md"
    assert docs[0].metadata["_document_id"]
    assert docs[0].metadata["_content_hash"]


@pytest.mark.asyncio
async def test_same_content_with_different_names_keeps_two_document_identities(documents):
    _, current = documents
    await upload("checkout-api.md", "shared instructions")
    await upload("billing-api.md", "shared instructions")
    assert len(current) == 2
    assert {docs[0].metadata["_file_name"] for docs in current.values()} == {
        "checkout-api.md",
        "billing-api.md",
    }


@pytest.mark.asyncio
async def test_failed_update_keeps_previous_vectors_and_active_manifest(documents):
    folder, current = documents
    await upload("checkout-api.md", "trusted instructions")
    old = dict(current)
    with pytest.raises(HTTPException) as error:
        await upload("checkout-api.md", "FAIL replacement")
    assert error.value.status_code == 500
    assert current == old
    manifests = list((folder / ".documents").glob("*.json"))
    assert len(manifests) == 1
    active = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert (folder / active["storage_path"]).read_text() == "trusted instructions"


@pytest.mark.asyncio
async def test_directory_reindex_uses_only_active_versions(documents):
    folder, current = documents
    await upload("checkout-api.md", "old instructions")
    await upload("checkout-api.md", "new instructions")
    current.clear()
    result = index_module.VectorIndexService().index_directory(str(folder))
    assert result.success
    assert result.total_files == 1
    assert len(current) == 1
    doc = next(iter(current.values()))[0]
    assert doc.page_content == "new instructions"
    assert doc.metadata["_file_name"] == "checkout-api.md"


@pytest.mark.asyncio
async def test_internal_object_directory_cannot_reintroduce_old_versions(documents):
    folder, current = documents
    await upload("checkout-api.md", "old instructions")
    await upload("checkout-api.md", "new instructions")
    before = dict(current)
    result = index_module.VectorIndexService().index_directory(str(folder / ".objects"))
    assert not result.success
    assert current == before


@pytest.mark.asyncio
async def test_uncertain_commit_preserves_selected_blob_for_reconciliation(documents, monkeypatch):
    from app.services.vector_store_manager import IndexCommitUncertain

    folder, current = documents
    await upload("checkout-api.md", "old instructions")

    def uncertain(*args, **kwargs):
        raise IndexCommitUncertain("commit response lost")

    monkeypatch.setattr(index_module.vector_store_manager, "replace_documents", uncertain)
    with pytest.raises(HTTPException) as error:
        await upload("checkout-api.md", "new instructions")
    assert error.value.status_code == 503
    assert error.value.detail["index_status"] == "pending_reconciliation"
    manifest = json.loads(next((folder / ".documents").glob("*.json")).read_text(encoding="utf-8"))
    assert (folder / manifest["storage_path"]).read_text() == "new instructions"


@pytest.mark.asyncio
async def test_outer_commit_failure_is_pending_reconciliation(documents, monkeypatch):
    from contextlib import contextmanager

    folder, _ = documents
    await upload("checkout-api.md", "old instructions")

    @contextmanager
    def lost_commit(_):
        yield None
        raise RuntimeError("commit reply lost")

    monkeypatch.setattr(index_module, "upload_document_lock", lost_commit)
    with pytest.raises(HTTPException) as error:
        await upload("checkout-api.md", "new instructions")
    assert error.value.status_code == 503
    manifest = json.loads(next((folder / ".documents").glob("*.json")).read_bytes())
    assert (folder / manifest["storage_path"]).read_text() == "new instructions"


@pytest.mark.asyncio
async def test_lost_lock_rollback_does_not_overwrite_later_same_content_attempt(
    documents, monkeypatch
):
    folder, _ = documents
    await upload("checkout-api.md", "old instructions")
    manifest_path = next((folder / ".documents").glob("*.json"))
    winner = None

    def failed_index(*args, **kwargs):
        nonlocal winner
        # Another writer can acquire the document lock after our connection dies.
        selected = json.loads(manifest_path.read_bytes())
        selected["attempt"] = "later-successful-request"
        winner = json.dumps(selected).encode()
        manifest_path.write_bytes(winner)
        raise RuntimeError("coordinator connection lost")

    monkeypatch.setattr(index_module.vector_store_manager, "replace_documents", failed_index)
    with pytest.raises(HTTPException):
        await upload("checkout-api.md", "new instructions")
    assert manifest_path.read_bytes() == winner


def test_directory_rejects_resolved_file_outside_selected_root(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "allowed"
    root.mkdir()
    link = root / "linked.md"
    link.write_text("placeholder")
    outside = tmp_path / "private.md"
    outside.write_text("must not be indexed")
    resolve = Path.resolve
    monkeypatch.setattr(
        Path, "resolve", lambda path, *a, **kw: outside if path == link else resolve(path, *a, **kw)
    )
    indexed = []
    service = index_module.VectorIndexService()
    monkeypatch.setattr(service, "index_single_file", indexed.append)
    result = service.index_directory(str(root))
    assert not result.success
    assert result.fail_count == 1
    assert indexed == []


def test_directory_rejects_manifest_link_outside_selected_root(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "allowed"
    (root / ".documents").mkdir(parents=True)
    link = root / ".documents" / "document.json"
    link.write_text("{}")
    outside = tmp_path / "private.json"
    outside.write_text("{}")
    resolve = Path.resolve
    monkeypatch.setattr(
        Path, "resolve", lambda path, *a, **kw: outside if path == link else resolve(path, *a, **kw)
    )
    indexed = []
    service = index_module.VectorIndexService()
    monkeypatch.setattr(service, "_reindex_manifest", indexed.append)
    result = service.index_directory(str(root))
    assert not result.success
    assert indexed == []


@pytest.mark.asyncio
async def test_upload_and_directory_rebuild_share_document_transaction(documents, monkeypatch):
    from contextlib import contextmanager

    folder, _ = documents
    owner, observed = object(), []
    in_transaction = False

    @contextmanager
    def document_lock(_):
        nonlocal in_transaction
        in_transaction = True
        try:
            yield owner
        finally:
            in_transaction = False

    def index(source, docs, *, coordinator, after_commit):
        assert coordinator is owner
        assert in_transaction
        after_commit.append(lambda: observed.append(in_transaction))

    monkeypatch.setattr(index_module, "upload_document_lock", document_lock)
    monkeypatch.setattr(index_module.vector_store_manager, "replace_documents", index)
    await upload("checkout-api.md", "instructions")
    assert index_module.VectorIndexService().index_directory(str(folder)).success
    assert observed == [False, False]
