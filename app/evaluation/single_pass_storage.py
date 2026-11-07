"""Local-only durability operations. Never retry a model/provider operation."""

import json
import os
import tempfile
import time
from pathlib import Path


class PersistenceError(RuntimeError):
    """Fixed, value-free error; a caller must stop before dispatching another case."""

    def __init__(self, code, cause=None):
        super().__init__(code)
        self.code = code
        self.storage_error_type = (
            getattr(cause, "storage_error_type", type(cause).__name__)
            if cause is not None
            else None
        )


def _permission_retry(operation):
    for attempt in range(4):
        try:
            return operation()
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * 2**attempt)


def _durable_temp(path, body):
    # Same directory keeps publication on one filesystem. A complete temporary
    # file remains available if publication ultimately fails.
    fd, name = _permission_retry(
        lambda: tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    return Path(name)


def atomic_json_write(path, value):
    path = Path(path)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    try:
        temporary = _durable_temp(path, body)
    except OSError as exc:
        raise PersistenceError("report_write_failed", exc) from None
    try:
        _permission_retry(lambda: temporary.replace(path))
    except OSError as exc:
        raise PersistenceError("report_replace_failed", exc) from None


def publish_once(path, body):
    """Publish a fully fsynced immutable receipt; never replace an existing one."""
    path = Path(path)
    try:
        _permission_retry(lambda: path.parent.mkdir(parents=True, exist_ok=True))
        temporary = _durable_temp(path, body)
        published = False
        try:
            # Atomic no-clobber publication on NTFS/POSIX; source and target are
            # in the same directory. Unsupported filesystems fail closed.
            _permission_retry(lambda: os.link(temporary, path))
            published = True
        except FileExistsError:
            if _permission_retry(path.read_bytes) != body:
                raise PersistenceError("durable_result_conflict") from None
            published = True
        finally:
            if published:
                try:
                    temporary.unlink()
                except OSError:
                    pass  # A leftover temporary file is not a published receipt.
    except OSError as exc:
        raise PersistenceError("result_snapshot_failed", exc) from None
