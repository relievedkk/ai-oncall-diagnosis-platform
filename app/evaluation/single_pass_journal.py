"""Durable, atomic attempt reservation. Unknown outcomes are never resent."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.single_pass import canonical
from app.evaluation.single_pass_storage import PersistenceError, publish_once


class AttemptJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS experiments (
                experiment TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                experiment TEXT NOT NULL, case_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                token TEXT NOT NULL, state TEXT NOT NULL, reserved_at TEXT NOT NULL,
                result TEXT, PRIMARY KEY(experiment, case_id))""")

    def bind_experiment(self, experiment_id, fingerprint):
        """Bind the entire experiment before any case can dispatch a request."""
        if not experiment_id or len(experiment_id) > 128 or not fingerprint:
            raise ValueError("invalid_experiment_id")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT fingerprint FROM experiments WHERE experiment=?", (experiment_id,)
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("configuration_mismatch")
                return
            if db.execute(
                "SELECT 1 FROM attempts WHERE experiment=? LIMIT 1", (experiment_id,)
            ).fetchone():
                raise ValueError("unbound_existing_experiment")
            db.execute("INSERT INTO experiments VALUES (?, ?)", (experiment_id, fingerprint))

    @contextmanager
    def connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except sqlite3.Error as exc:
            raise PersistenceError("attempt_journal_failed", exc) from None
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error as exc:
                    raise PersistenceError("attempt_journal_failed", exc) from None

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
        if (
            not isinstance(result, dict)
            or result.get("status")
            not in {"supported", "candidate", "insufficient_evidence", "error"}
            or result.get("case_id", case_id) != case_id
        ):
            raise PersistenceError("durable_result_invalid")
        fingerprint = result.get("configuration_sha256")
        read_failure = None
        try:
            row = self.load(experiment_id, case_id)
        except PersistenceError as exc:
            # Preserve a server-bound result even if SQLite cannot be read now.
            if not isinstance(fingerprint, str) or not fingerprint:
                raise
            read_failure = exc
        else:
            if (
                row is None
                or row["token"] != token
                or fingerprint is not None
                and fingerprint != row["fingerprint"]
            ):
                raise PersistenceError("invalid_reservation_owner")
            fingerprint = row["fingerprint"]
        record = {
            "version": 1,
            "experiment": experiment_id,
            "case_id": case_id,
            "token": token,
            "fingerprint": fingerprint,
            "result": result,
        }
        envelope = {
            "record": record,
            "sha256": hashlib.sha256(canonical(record).encode()).hexdigest(),
        }
        publish_once(
            self._receipt_path(experiment_id, case_id, token, fingerprint),
            canonical(envelope).encode(),
        )
        if read_failure is not None:
            raise PersistenceError("result_commit_failed", read_failure) from None
        self._commit_result(experiment_id, case_id, token, fingerprint, result)

    def _receipt_path(self, experiment_id, case_id, token, fingerprint):
        key = hashlib.sha256(
            canonical([experiment_id, case_id, token, fingerprint]).encode()
        ).hexdigest()
        return self.path.parent / (self.path.name + ".results") / (key + ".json")

    def _commit_result(self, experiment_id, case_id, token, fingerprint, result):
        state = "complete" if result["status"] != "error" else "failed_unknown"
        serialized = canonical(result)
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM attempts WHERE experiment=? AND case_id=?",
                    (experiment_id, case_id),
                ).fetchone()
                if row is None or row["token"] != token or row["fingerprint"] != fingerprint:
                    raise PersistenceError("invalid_reservation_owner")
                if row["state"] != "reserved":
                    if row["state"] == state and row["result"] == serialized:
                        return  # A previous commit may have succeeded before an I/O error.
                    raise PersistenceError("invalid_reservation_owner")
                db.execute(
                    "UPDATE attempts SET state=?, result=? WHERE experiment=? AND case_id=?",
                    (state, serialized, experiment_id, case_id),
                )
        except sqlite3.Error as exc:
            raise PersistenceError("result_commit_failed", exc) from None
        except PersistenceError as exc:
            if exc.code == "attempt_journal_failed":
                raise PersistenceError("result_commit_failed", exc) from None
            raise

    def recover(self, experiment_id, case_id, fingerprint):
        """Explicit resume reconciles a receipt, never a missing provider response."""
        row = self.load(experiment_id, case_id)
        if row is None or row["fingerprint"] != fingerprint:
            raise ValueError("configuration_mismatch")
        if row["state"] != "reserved":
            return row
        path = self._receipt_path(experiment_id, case_id, row["token"], fingerprint)
        try:
            envelope = json.loads(path.read_bytes())
        except FileNotFoundError:
            return row  # Includes historical case-026: no receipt, no resend.
        except (OSError, ValueError, RecursionError) as exc:
            raise PersistenceError("durable_result_invalid", exc) from None
        try:
            record = envelope["record"]
            result = record["result"]
            valid = (
                record["version"] == 1
                and record["experiment"] == experiment_id
                and record["case_id"] == case_id
                and record["token"] == row["token"]
                and record["fingerprint"] == fingerprint
                and hashlib.sha256(canonical(record).encode()).hexdigest() == envelope["sha256"]
                and isinstance(result, dict)
                and result["status"] in {"supported", "candidate", "insufficient_evidence", "error"}
                and result.get("configuration_sha256", fingerprint) == fingerprint
                and result.get("case_id", case_id) == case_id
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise PersistenceError("durable_result_invalid")
        self._commit_result(experiment_id, case_id, row["token"], fingerprint, result)
        return self.load(experiment_id, case_id)

    @staticmethod
    def cached(row):
        try:
            result = json.loads(row["result"])
            if not isinstance(result, dict) or result.get("status") not in {
                "supported",
                "candidate",
                "insufficient_evidence",
                "error",
            }:
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise PersistenceError("journal_result_invalid", exc) from None
