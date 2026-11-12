"""Per-user durable identity, independent of copied task/run directories."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from app.evaluation.heldout_dataset import digest

REGISTRY_ROOT = Path.home() / ".oncall-evaluation" / "grouped-holdout"


@contextmanager
def connection():
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(REGISTRY_ROOT / "registry.sqlite", timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bindings (dataset TEXT PRIMARY KEY, run TEXT NOT NULL, retired INTEGER NOT NULL, final_sha TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS contents (identity TEXT PRIMARY KEY, dataset TEXT NOT NULL)"
            )
            yield db
    finally:
        db.close()


def claim(public, run_dir):
    try:
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO bindings VALUES (?,?,0,NULL)", (public["dataset_id"], str(run_dir))
            )
            for content in public["content_sha256"].values():
                identity = digest([public["source_revision"], content])
                db.execute("INSERT INTO contents VALUES (?,?)", (identity, public["dataset_id"]))
    except sqlite3.IntegrityError:
        raise ValueError("dataset_already_bound_or_retired") from None


def binding(dataset_id):
    with connection() as db:
        row = db.execute("SELECT * FROM bindings WHERE dataset=?", (dataset_id,)).fetchone()
    if row is None:
        raise ValueError("dataset_binding_missing")
    return dict(row)


def retire(dataset_id, final_sha):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM bindings WHERE dataset=?", (dataset_id,)).fetchone()
        if row is None or row["final_sha"] not in {None, final_sha}:
            raise ValueError("retirement_mismatch")
        db.execute(
            "UPDATE bindings SET retired=1, final_sha=? WHERE dataset=?", (final_sha, dataset_id)
        )


@contextmanager
def exclusive(dataset_id):
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(REGISTRY_ROOT / (digest(dataset_id) + ".lock")), timeout=0):
            yield
    except Timeout:
        raise ValueError("evaluation_batch_busy") from None


def journal_path():
    return REGISTRY_ROOT / "attempts.sqlite"
