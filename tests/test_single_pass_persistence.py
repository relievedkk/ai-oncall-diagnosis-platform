import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.aiops.single_pass import run_single_pass
from app.evaluation.single_pass_journal import AttemptJournal
from app.evaluation.single_pass_runner import _write
from app.evaluation.single_pass_storage import PersistenceError
from tests.single_pass_helpers import choice_of
from tests.test_single_pass_execution import Model, packet


def test_report_replace_recovers_transient_windows_sharing_violation(tmp_path, monkeypatch):
    target = tmp_path / "summary.json"
    target.write_text('{"old":true}')
    original = Path.replace
    failures = [PermissionError(13, "synthetic sharing violation")]

    def replace(path, destination):
        if Path(destination) == target and failures:
            raise failures.pop()
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    _write(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_report_persistent_replace_failure_preserves_old_and_complete_recovery_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "summary.json"
    target.write_text('{"old":true}')
    failures = []

    def replace(path, destination):
        failures.append(str(path))
        raise PermissionError(13, "synthetic persistent denial")

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PersistenceError, match="report_replace_failed"):
        _write(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert 1 < len(failures) <= 5
    assert any(json.loads(p.read_text()) == {"new": True} for p in tmp_path.glob("*.tmp"))


class FailResultCommit(AttemptJournal):
    """Real SQLite transaction rollback after a real model response, not a mocked journal."""

    @contextmanager
    def connection(self):
        with super().connection() as db:
            yield db
            row = db.execute("SELECT COUNT(*) FROM attempts WHERE state!='reserved'").fetchone()
            if row[0]:
                raise sqlite3.OperationalError("synthetic disk I/O error at result commit")


@pytest.mark.asyncio
async def test_response_survives_sqlite_commit_failure_and_restart_without_new_dispatch(tmp_path):
    p = packet()
    model = Model(choice_of(p))
    path = tmp_path / "attempts.sqlite"
    kwargs = {
        "experiment_id": "persist",
        "config": {"model": "test"},
        "model_factory": lambda: model,
    }
    with pytest.raises(PersistenceError, match="result_commit_failed"):
        await run_single_pass(p, journal=FailResultCommit(path), **kwargs)
    assert model.calls == 1
    reopened = AttemptJournal(path)
    assert reopened.load("persist", p.case_id)["state"] == "reserved"
    result = await run_single_pass(p, journal=reopened, resume=True, **kwargs)
    assert result["status"] == "supported" and result["usage"]["total_tokens"] == 25
    assert result["reused"] and result["new_model_calls"] == 0 and model.calls == 1
    assert reopened.load("persist", p.case_id)["state"] == "complete"


@pytest.mark.asyncio
async def test_result_commit_failure_stops_batch_before_truth_or_second_dispatch(tmp_path):
    from app.evaluation.single_pass_batch import evaluate_cases
    from tests.single_pass_helpers import view_of

    def open_view(cid):
        view = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
        view.case_id = cid
        return view

    pilot = SimpleNamespace(
        ids={"case-001", "case-002"},
        open=open_view,
        truth=lambda _: pytest.fail("labels read after persistence failed"),
    )
    models = []

    def factory(p):
        model = Model(choice_of(p))
        models.append(model)
        return model

    with pytest.raises(PersistenceError, match="result_commit_failed"):
        await evaluate_cases(
            pilot,
            output_dir=tmp_path,
            journal=FailResultCommit(tmp_path / "attempts.sqlite"),
            experiment_id="closed",
            config={},
            model_factory=factory,
            telemetry_profile="rcaeval-re2",
            timeout_seconds=3,
        )
    assert sum(m.calls for m in models) == 1


@pytest.mark.asyncio
async def test_database_unreadable_after_response_still_preserves_result_receipt(
    tmp_path, monkeypatch
):
    p = packet()
    path = tmp_path / "j.sqlite"
    journal = AttemptJournal(path)

    with monkeypatch.context() as patch:

        class DatabaseUnavailable(Model):
            async def ainvoke(self, messages):
                response = await super().ainvoke(messages)

                def fail(*args, **kwargs):
                    raise sqlite3.OperationalError("synthetic unavailable database")

                patch.setattr(sqlite3, "connect", fail)
                return response

        model = DatabaseUnavailable(choice_of(p))
        with pytest.raises(PersistenceError, match="result_commit_failed"):
            await run_single_pass(
                p,
                journal=journal,
                experiment_id="unreadable",
                config={},
                model_factory=lambda: model,
            )

    result = await run_single_pass(
        p,
        journal=AttemptJournal(path),
        experiment_id="unreadable",
        config={},
        resume=True,
        model_factory=lambda: pytest.fail("model recreated"),
    )
    assert result["usage"]["total_tokens"] == 25 and result["status"] == "supported"
    assert result["new_model_calls"] == 0 and model.calls == 1


@pytest.mark.asyncio
async def test_post_recovery_read_failure_stops_batch_even_when_commit_succeeded(
    tmp_path, monkeypatch
):
    from app.evaluation.single_pass_batch import evaluate_cases

    p = packet()
    path = tmp_path / "j.sqlite"
    with pytest.raises(PersistenceError, match="result_commit_failed"):
        await run_single_pass(
            p,
            journal=FailResultCommit(path),
            experiment_id="reload",
            config={},
            model_factory=lambda: Model(choice_of(p)),
        )
    journal = AttemptJournal(path)
    real_connect = sqlite3.connect
    count = 0

    def transient_after_commit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 4:  # engine load, recover load, commit, recover reload
            raise sqlite3.OperationalError("synthetic recovery reload failure")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", transient_after_commit)
    pilot = SimpleNamespace(
        ids={p.case_id, "next"}, truth=lambda _: pytest.fail("labels read after reload failed")
    )
    with pytest.raises(PersistenceError, match="attempt_journal_failed"):
        await evaluate_cases(
            pilot,
            output_dir=tmp_path,
            journal=journal,
            experiment_id="reload",
            config={},
            prepared_packets={p.case_id: p},
            resume=True,
            model_factory=lambda _: pytest.fail("next model created"),
            telemetry_profile="rcaeval-re2",
            timeout_seconds=3,
        )
    with closing(real_connect(path)) as db, db:
        assert (
            db.execute("SELECT state FROM attempts WHERE experiment='reload'").fetchone()[0]
            == "complete"
        )


def test_wrong_fingerprint_cannot_poison_the_valid_reservations_receipt(tmp_path):
    journal = AttemptJournal(tmp_path / "j.sqlite")
    token = journal.reserve("run", "case", "correct-fingerprint")
    with pytest.raises(PersistenceError, match="invalid_reservation_owner"):
        journal.finish(
            "run", "case", token, {"status": "supported", "configuration_sha256": "wrong"}
        )
    journal.finish(
        "run", "case", token, {"status": "supported", "configuration_sha256": "correct-fingerprint"}
    )
    assert journal.load("run", "case")["state"] == "complete"


@pytest.mark.asyncio
async def test_sqlite_read_failure_stops_batch_before_any_model_dispatch(tmp_path, monkeypatch):
    from app.evaluation.single_pass_batch import evaluate_cases
    from tests.single_pass_helpers import view_of

    journal = AttemptJournal(tmp_path / "j.sqlite")

    def cannot_open(*args, **kwargs):
        raise sqlite3.OperationalError("SECRET synthetic unavailable database")

    monkeypatch.setattr(sqlite3, "connect", cannot_open)
    v = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
    v.case_id = "case-001"
    pilot = SimpleNamespace(
        ids={"case-001", "case-002"},
        open=lambda _: v,
        truth=lambda _: pytest.fail("labels read after storage failed"),
    )
    with pytest.raises(PersistenceError, match="attempt_journal_failed") as error:
        await evaluate_cases(
            pilot,
            output_dir=tmp_path,
            journal=journal,
            experiment_id="read",
            config={},
            model_factory=lambda _: pytest.fail("model constructed"),
            telemetry_profile="rcaeval-re2",
            timeout_seconds=3,
        )
    assert "SECRET" not in str(error.value)
    assert not (tmp_path / "case-002-prediction.json").exists()


@pytest.mark.asyncio
async def test_legacy_unknown_without_receipt_never_dispatches(tmp_path):
    from app.agent.aiops.single_pass import configuration_fingerprint

    p = packet()
    journal = AttemptJournal(tmp_path / "j.sqlite")
    journal.reserve("legacy", p.case_id, configuration_fingerprint(p, {}))
    with pytest.raises(ValueError, match="attempt_already_reserved"):
        await run_single_pass(
            p,
            journal=journal,
            experiment_id="legacy",
            config={},
            resume=True,
            model_factory=lambda: pytest.fail("unknown case was resent"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["truncated", "checksum", "case", "fingerprint", "token"])
async def test_invalid_receipt_cannot_turn_unknown_into_success_or_resend(tmp_path, mutation):
    import hashlib

    from app.core.single_pass import canonical

    p = packet()
    model = Model(choice_of(p))
    journal = FailResultCommit(tmp_path / "j.sqlite")
    with pytest.raises(PersistenceError, match="result_commit_failed"):
        await run_single_pass(
            p, journal=journal, experiment_id="damaged", config={}, model_factory=lambda: model
        )
    (receipt,) = list(tmp_path.glob("*.results/*.json"))
    data = json.loads(receipt.read_text())
    if mutation == "truncated":
        receipt.write_text("{")
    else:
        if mutation == "checksum":
            data["record"]["result"]["usage"]["total_tokens"] = 999
        else:
            data["record"][
                {"case": "case_id", "fingerprint": "fingerprint", "token": "token"}[mutation]
            ] = "wrong"
            # Even a self-consistent checksum must not override reservation ownership.
            data["sha256"] = hashlib.sha256(canonical(data["record"]).encode()).hexdigest()
        receipt.write_text(json.dumps(data))
    with pytest.raises(PersistenceError, match="durable_result_invalid"):
        await run_single_pass(
            p,
            journal=AttemptJournal(tmp_path / "j.sqlite"),
            experiment_id="damaged",
            config={},
            resume=True,
            model_factory=lambda: pytest.fail("invalid receipt was resent"),
        )
    assert model.calls == 1


def test_immutable_receipt_rejects_conflicting_finish(tmp_path):
    journal = AttemptJournal(tmp_path / "j.sqlite")
    token = journal.reserve("run", "case", "f")
    journal.finish("run", "case", token, {"status": "supported", "service": "a"})
    with pytest.raises(PersistenceError, match="durable_result_conflict"):
        journal.finish("run", "case", token, {"status": "supported", "service": "wrong"})
    assert journal.cached(journal.load("run", "case"))["service"] == "a"


def test_identical_finish_can_be_reconciled_idempotently(tmp_path):
    journal = AttemptJournal(tmp_path / "j.sqlite")
    token = journal.reserve("run", "case", "f")
    result = {"status": "supported", "service": "a"}
    journal.finish("run", "case", token, result)
    journal.finish("run", "case", token, result)
    assert journal.cached(journal.load("run", "case")) == result


@pytest.mark.asyncio
async def test_recovered_parse_error_preserves_cost_but_never_retries_model(tmp_path):
    p = packet()
    model = Model()  # One observed response with a parse failure.
    kwargs = {"experiment_id": "failed", "config": {}, "model_factory": lambda: model}
    with pytest.raises(PersistenceError, match="result_commit_failed"):
        await run_single_pass(p, journal=FailResultCommit(tmp_path / "j.sqlite"), **kwargs)
    reopened = AttemptJournal(tmp_path / "j.sqlite")
    with pytest.raises(ValueError, match="attempt_already_reserved"):
        await run_single_pass(p, journal=reopened, resume=True, **kwargs)
    row = reopened.load("failed", p.case_id)
    assert row["state"] == "failed_unknown"
    cached = reopened.cached(row)
    assert cached["failure_detail_code"] == "schema_parse_failed"
    assert cached["usage"]["total_tokens"] == 25 and model.calls == 1


def test_unpublished_receipt_retains_fsynced_result_on_permanent_link_failure(
    tmp_path, monkeypatch
):
    import os

    from app.evaluation.single_pass_storage import publish_once

    def fail(*args):
        raise PermissionError(13, "publication denied")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(PersistenceError, match="result_snapshot_failed"):
        publish_once(tmp_path / "receipt.json", b'{"paid_result":true}')
    assert not (tmp_path / "receipt.json").exists()
    assert any(p.read_bytes() == b'{"paid_result":true}' for p in tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_cancellation_propagates_even_if_result_commit_fails(tmp_path):
    import asyncio

    p = packet()
    model = Model(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_single_pass(
            p,
            journal=FailResultCommit(tmp_path / "j.sqlite"),
            experiment_id="cancel",
            config={},
            model_factory=lambda: model,
        )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_changed_reservation_owner_stops_batch_before_next_paid_case(tmp_path):
    from app.evaluation.single_pass_batch import evaluate_cases
    from tests.single_pass_helpers import view_of

    path = tmp_path / "j.sqlite"
    journal = AttemptJournal(path)
    models = []

    class OwnershipChanged(Model):
        async def ainvoke(self, messages):
            response = await super().ainvoke(messages)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE attempts SET token='other-owner'")
            return response

    def view(cid):
        v = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
        v.case_id = cid
        return v

    def factory(p):
        m = OwnershipChanged(choice_of(p))
        models.append(m)
        return m

    pilot = SimpleNamespace(
        ids={"case-001", "case-002"},
        open=view,
        truth=lambda _: pytest.fail("labels read after owner mismatch"),
    )
    with pytest.raises(PersistenceError, match="invalid_reservation_owner"):
        await evaluate_cases(
            pilot,
            output_dir=tmp_path,
            journal=journal,
            experiment_id="owner",
            config={},
            model_factory=factory,
            telemetry_profile="rcaeval-re2",
            timeout_seconds=3,
        )
    assert sum(m.calls for m in models) == 1
