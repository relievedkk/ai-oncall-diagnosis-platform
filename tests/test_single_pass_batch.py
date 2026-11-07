import hashlib
import importlib.util
import json
from types import SimpleNamespace

import pytest

from app.core.single_pass import REVISION


def batch_module():
    assert importlib.util.find_spec("app.evaluation.single_pass_batch") is not None
    from app.evaluation import single_pass_batch

    return single_pass_batch


def test_v4_selection_is_rejected_before_creating_outputs(tmp_path):
    from app.evaluation.rcaeval import main

    out = tmp_path / "out"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--protocol-version",
                "4",
                "--mode",
                "validate",
                "--output-dir",
                str(out),
                "--data-dir",
                str(tmp_path / "missing"),
            ]
        )
    assert error.value.code == 2
    assert not out.exists()


def test_v51_scope_does_not_depend_on_retired_runner(tmp_path):
    assert importlib.util.find_spec("app.evaluation.evaluation_scope") is not None
    from app.evaluation.evaluation_scope import DevelopmentScope

    answer = tmp_path / "answers.parquet"
    answer.touch()
    scope = DevelopmentScope(answer)
    assert "case-001" in scope.case_ids and "case-002" not in scope.case_ids
    with pytest.raises(ValueError):
        DevelopmentScope(answer, frozenset({"case-002"}))


def test_all54_scope_requires_complete_exact_anonymous_ids():
    batch = batch_module()
    ids = {f"case-{i:03}" for i in range(1, 55)}
    batch.validate_case_ids(ids)
    for bad in (ids - {"case-054"}, ids | {"case-055"}, (ids - {"case-001"}) | {"SECRET"}):
        with pytest.raises(ValueError, match="scope"):
            batch.validate_case_ids(bad)


def test_low_feasibility_measurement_requires_matching_review_and_explicit_ack():
    batch = batch_module()
    from app.core.single_pass import canonical

    audit = {"paid_gate_passed": False, "audited": 18, "errors": 0}
    digest = hashlib.sha256(canonical(audit).encode()).hexdigest()
    with pytest.raises(ValueError):
        batch.approve_execution(audit, reviewed_sha=digest, acknowledge=False)
    with pytest.raises(ValueError):
        batch.approve_execution(audit, reviewed_sha="wrong", acknowledge=True)
    assert batch.approve_execution(audit, reviewed_sha=digest, acknowledge=True) is True
    broken = {**audit, "errors": 1}
    with pytest.raises(ValueError):
        batch.approve_execution(broken, reviewed_sha=digest, acknowledge=True)


@pytest.mark.asyncio
async def test_real_v51_batch_persists_prediction_before_truth_and_resumes_without_calls(tmp_path):
    batch = batch_module()
    from app.agent.aiops.single_pass import SinglePassChoice
    from app.evaluation.single_pass_journal import AttemptJournal
    from tests.single_pass_helpers import view_of
    from tests.test_single_pass_execution import Model

    view = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
    view.case_id = "case-001"
    models = []
    output = tmp_path / "results"
    output.mkdir()

    def factory(packet):
        result = SinglePassChoice(hypothesis_id=None, decision="unknown")
        model = Model(result)
        models.append(model)
        return model

    def truth(case_id):
        raw = json.loads((output / f"{case_id}-prediction.json").read_text("utf-8"))
        assert raw["status"] == "insufficient_evidence"
        assert "ground_truth" not in raw and "score" not in raw
        assert sum(m.calls for m in models) == 1
        return {"root_cause_service": "a", "fault": "cpu", "system": "test"}

    pilot = SimpleNamespace(ids={"case-001": "case-001"}, open=lambda _: view, truth=truth)
    kwargs = {
        "output_dir": output,
        "journal": AttemptJournal(tmp_path / "journal.sqlite"),
        "experiment_id": "v51-test",
        "config": {"model": "test"},
        "model_factory": factory,
        "telemetry_profile": "rcaeval-re2",
        "timeout_seconds": 3,
    }
    first = await batch.evaluate_cases(pilot, **kwargs)
    assert first["case_count"] == 1 and first["joint_correct"] == 0
    assert first["insufficient_evidence"] == 1 and first["revision"] == REVISION
    assert first["complete"] is True and first["reserved_attempts"] == 1
    second = await batch.evaluate_cases(pilot, resume=True, **kwargs)
    assert second["reserved_attempts"] == 1 and sum(m.calls for m in models) == 1


@pytest.mark.asyncio
async def test_provider_failure_is_counted_and_never_resent_on_resume(tmp_path):
    batch = batch_module()
    from app.evaluation.single_pass_journal import AttemptJournal
    from tests.single_pass_helpers import view_of
    from tests.test_single_pass_execution import Model

    view = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
    view.case_id = "case-001"
    model = Model(error=RuntimeError("SECRET"))
    pilot = SimpleNamespace(
        ids={"case-001": "case-001"},
        open=lambda _: view,
        truth=lambda _: {"root_cause_service": "a", "fault": "cpu"},
    )
    kwargs = {
        "output_dir": tmp_path,
        "journal": AttemptJournal(tmp_path / "journal.sqlite"),
        "experiment_id": "failed",
        "config": {},
        "model_factory": lambda _: model,
        "telemetry_profile": "rcaeval-re2",
        "timeout_seconds": 3,
    }
    first = await batch.evaluate_cases(pilot, **kwargs)
    original_prediction = (tmp_path / "case-001-prediction.json").read_bytes()
    second = await batch.evaluate_cases(pilot, resume=True, **kwargs)
    assert first["failed"] == second["failed"] == 1
    assert second["case_count"] == second["reserved_attempts"] == 1
    assert model.calls == 1 and "SECRET" not in json.dumps(second)
    assert (tmp_path / "case-001-prediction.json").read_bytes() == original_prediction


@pytest.mark.parametrize("extra", [[], ["--allow-live"], ["--max-calls", "2"]])
def test_full_evaluation_cli_rejects_incomplete_live_authority_before_dataset_access(
    tmp_path, extra
):
    batch = batch_module()
    out = tmp_path / "never-created"
    with pytest.raises(SystemExit) as error:
        batch.main(["--mode", "live", "--output-dir", str(out), *extra])
    assert error.value.code == 2 and not out.exists()


def test_invalid_acknowledgment_stops_cli_before_experiment_manifest(tmp_path, monkeypatch):
    batch = batch_module()
    answers = tmp_path / "answers.parquet"
    answers.touch()
    out = tmp_path / "out"
    monkeypatch.setattr(batch, "DEVELOPMENT_ANSWERS", answers)
    monkeypatch.setattr(
        batch,
        "Pilot",
        lambda *a, **kw: SimpleNamespace(
            ids={f"case-{i:03}": f"case-{i:03}" for i in range(1, 55)}, dataset="RCAEval RE2 test"
        ),
    )
    monkeypatch.setattr(
        batch,
        "obtain_preflight",
        lambda *a, **kw: {
            "audit": {"audited": 18, "errors": 0, "paid_gate_passed": False},
            "audit_sha256": "new-audit-sha",
            "telemetry_validation": {"passed": True, "case_count": 54},
        },
    )
    with pytest.raises(SystemExit) as error:
        batch.main(
            [
                "--mode",
                "live",
                "--output-dir",
                str(out),
                "--allow-live",
                "--experiment-id",
                "test",
                "--reviewed-audit-sha256",
                "wrong",
                "--accept-low-feasibility",
            ]
        )
    assert error.value.code == 2
    assert (out / "development-audit.json").exists()
    assert not (out / "experiment.json").exists()


def test_experiment_registry_rejects_changed_configuration_and_legacy_attempts(tmp_path):
    from app.evaluation.single_pass_journal import AttemptJournal

    journal = AttemptJournal(tmp_path / "registry.sqlite")
    assert hasattr(journal, "bind_experiment")
    journal.bind_experiment("run", "config-one")
    journal.bind_experiment("run", "config-one")
    with pytest.raises(ValueError, match="configuration_mismatch"):
        journal.bind_experiment("run", "config-two")
    journal.reserve("legacy", "case-001", "packet-fingerprint")
    with pytest.raises(ValueError, match="unbound_existing_experiment"):
        journal.bind_experiment("legacy", "new-config")


@pytest.mark.asyncio
async def test_changed_configuration_aborts_batch_instead_of_continuing_paid_cases(tmp_path):
    batch = batch_module()
    from app.evaluation.single_pass_journal import AttemptJournal
    from tests.single_pass_helpers import view_of
    from tests.test_single_pass_execution import Model

    view = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
    view.case_id = "case-001"
    journal = AttemptJournal(tmp_path / "attempts.sqlite")
    journal.reserve("changed", "case-001", "old-config")
    model = Model()
    pilot = SimpleNamespace(
        ids={"case-001": "case-001", "case-002": "case-002"},
        open=lambda _: view,
        truth=lambda _: pytest.fail("truth read"),
    )
    with pytest.raises(ValueError, match="configuration_mismatch"):
        await batch.evaluate_cases(
            pilot,
            output_dir=tmp_path,
            journal=journal,
            experiment_id="changed",
            config={"model": "changed"},
            model_factory=lambda _: model,
            telemetry_profile="rcaeval-re2",
            timeout_seconds=3,
            resume=True,
        )
    assert model.calls == 0 and not (tmp_path / "summary.json").exists()


def test_rejected_resume_preserves_original_reviewed_audit(tmp_path, monkeypatch):
    batch = batch_module()
    answers = tmp_path / "answers.parquet"
    answers.touch()
    out = tmp_path / "original"
    out.mkdir()
    old = b'{"original_reviewed_audit":true}'
    (out / "development-audit.json").write_bytes(old)
    (out / "experiment.json").write_text("{}")
    monkeypatch.setattr(batch, "DEVELOPMENT_ANSWERS", answers)
    monkeypatch.setattr(
        batch,
        "Pilot",
        lambda *a, **kw: SimpleNamespace(
            ids={f"case-{i:03}": f"case-{i:03}" for i in range(1, 55)}, dataset="RCAEval RE2 test"
        ),
    )
    monkeypatch.setattr(
        batch,
        "obtain_preflight",
        lambda *a, **kw: {
            "audit": {"audited": 18, "errors": 0, "paid_gate_passed": False},
            "audit_sha256": "new-audit-sha",
            "telemetry_validation": {"passed": True, "case_count": 54},
        },
    )
    with pytest.raises(SystemExit):
        batch.main(
            [
                "--mode",
                "live",
                "--output-dir",
                str(out),
                "--allow-live",
                "--resume",
                "--experiment-id",
                "test",
                "--reviewed-audit-sha256",
                "wrong",
                "--accept-low-feasibility",
            ]
        )
    assert (out / "development-audit.json").read_bytes() == old
