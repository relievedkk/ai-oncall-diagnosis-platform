"""Durable, atomic attempt reservation. Unknown outcomes are never resent."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.single_pass import canonical


class AttemptJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                experiment TEXT NOT NULL, case_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                token TEXT NOT NULL, state TEXT NOT NULL, reserved_at TEXT NOT NULL,
                result TEXT, PRIMARY KEY(experiment, case_id))""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self, experiment_id, case_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE experiment=? AND case_id=?", (experiment_id, case_id)
            ).fetchone()
        return dict(row) if row else None

    def reserve(self, experiment_id, case_id, fingerprint):
        if not experiment_id or len(experiment_id) > 128:
            raise ValueError("invalid_experiment_id")
        token = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT fingerprint FROM attempts WHERE experiment=? AND case_id=?",
                (experiment_id, case_id),
            ).fetchone()
            if row:
                raise ValueError(
                    "configuration_mismatch"
                    if row["fingerprint"] != fingerprint
                    else "attempt_already_reserved"
                )
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,NULL)",
                (
                    experiment_id,
                    case_id,
                    fingerprint,
                    token,
                    "reserved",
                    datetime.now(UTC).isoformat(),
                ),
            )
        return token

    def finish(self, experiment_id, case_id, token, result):
        state = "complete" if result["status"] != "error" else "failed_unknown"
        with self.connection() as db:
            changed = db.execute(
                "UPDATE attempts SET state=?, result=? WHERE experiment=? "
                "AND case_id=? AND token=? AND state='reserved'",
                (state, canonical(result), experiment_id, case_id, token),
            ).rowcount
            if changed != 1:
                raise ValueError("invalid_reservation_owner")

    @staticmethod
    def cached(row):
        return json.loads(row["result"])
