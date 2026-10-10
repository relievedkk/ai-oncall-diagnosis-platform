import io
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.api import file as file_api
from app.core.milvus_client import validate_vector_dimension


def test_vector_dimension_mismatch_never_drops_collection() -> None:
    with pytest.raises(RuntimeError, match="不会自动删除"):
        validate_vector_dimension(768, 1024)


def test_vector_dimension_match_is_accepted() -> None:
    validate_vector_dimension(1024, 1024)


@pytest.mark.asyncio
async def test_upload_reports_index_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)

    def fail_index(_: str) -> None:
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(file_api.vector_index_service, "index_single_file", fail_index)
    upload = UploadFile(filename="runbook.md", file=io.BytesIO(b"hello"))

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["index_status"] == "failed"
    assert not list(tmp_path.glob("*.md"))


@pytest.mark.asyncio
async def test_oversized_upload_does_not_delete_existing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(file_api.config, "max_upload_bytes", 4)
    existing = tmp_path / "runbook.md"
    existing.write_bytes(b"trusted")
    upload = UploadFile(filename="runbook.md", file=io.BytesIO(b"too large"))

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 413
    assert existing.read_bytes() == b"trusted"


@pytest.mark.asyncio
async def test_pdf_signature_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(file_api, "UPLOAD_DIR", tmp_path)
    upload = UploadFile(filename="fake.pdf", file=io.BytesIO(b"not-a-pdf"))

    with pytest.raises(HTTPException) as exc_info:
        await file_api.upload_file(upload)

    assert exc_info.value.status_code == 400
    assert not list(tmp_path.glob("*.pdf"))


@pytest.mark.asyncio
async def test_index_directory_preserves_forbidden_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_api, "is_path_allowed", lambda _: False)

    with pytest.raises(HTTPException) as exc_info:
        await file_api.index_directory("C:/Windows")

    assert exc_info.value.status_code == 403
