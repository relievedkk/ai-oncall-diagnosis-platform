from types import SimpleNamespace

import pytest

from app.evaluation.evaluation_scope import DevelopmentScope
from app.evaluation.single_pass_runner import audit_development, feasibility_gate, main
from tests.single_pass_helpers import view_of


def test_paid_gate_requires_both_independent_counts_and_complete_audit():
    audit = {
        "case_count": 18,
        "audited": 18,
        "candidate_pair_recall": 17,
        "eligible_correct_pairs": 17,
        "packet_passed": 18,
        "errors": 0,
    }
    assert feasibility_gate(audit)
    for key in ("audited", "candidate_pair_recall", "eligible_correct_pairs", "packet_passed"):
        assert not feasibility_gate({**audit, key: 16})
    assert not feasibility_gate({**audit, "case_count": 17})
    assert not feasibility_gate({**audit, "errors": 1})


def test_unapproved_scope_rejected_before_any_telemetry_or_answers(tmp_path):
    answer = tmp_path / "answers.parquet"
    answer.touch()
    pilot = SimpleNamespace(
        ids={"case-002": "ignored"},
        answers_file=answer,
        open=lambda _: pytest.fail("telemetry opened"),
        truth=lambda _: pytest.fail("truth opened"),
    )
    with pytest.raises(ValueError, match="scope"):
        audit_development(pilot, scope=DevelopmentScope(answer))


def test_features_variants_and_packets_are_frozen_before_truth(tmp_path, monkeypatch):
    from app.evaluation import single_pass_runner as runner

    answer = tmp_path / "answers.parquet"
    answer.touch()
    calls = []
    original = runner.collect_evidence
    view = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21}, incident=10)
    view.case_id = "case-001"

    def collect(*args, **kwargs):
        assert "truth" not in calls
        calls.append("features")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "collect_evidence", collect)

    def truth(_):
        assert len(calls) == 5  # base + +/-30/60 (invalid window perturbations recorded locally)
        calls.append("truth")
        return {"root_cause_service": "a", "fault": "cpu"}

    pilot = SimpleNamespace(
        ids={"case-001": "ignored"}, answers_file=answer, open=lambda _: view, truth=truth
    )
    audit = audit_development(pilot, scope=DevelopmentScope(answer))
    assert audit["model_calls"] == 0 and audit["case_count"] == 1
    assert audit["cases"][0]["ablations"]
    assert len(audit["cases"][0]["time_perturbations"]) == 4
    assert not audit["paid_gate_passed"]


def test_default_entry_routes_to_single_v5_without_loading_old_runner(monkeypatch):
    from app.evaluation import rcaeval, single_pass_runner

    captured = []
    monkeypatch.setattr(single_pass_runner, "main", lambda argv: captured.append(argv) or 123)
    assert rcaeval.main(["--mode", "validate"]) == 123
    assert captured == [["--mode", "validate"]]


@pytest.mark.parametrize(
    "argv",
    [
        ["--max-calls", "2"],
        ["--mode", "live"],
        ["--mode", "live", "--allow-live"],
        ["--timeout-seconds", "0"],
    ],
)
def test_unsafe_live_or_multiple_calls_fail_before_dataset_access(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_output_overwrite_is_rejected_before_dataset_access(tmp_path):
    (tmp_path / "result.json").write_text("existing")
    with pytest.raises(SystemExit):
        main(["--output-dir", str(tmp_path), "--data-dir", str(tmp_path / "missing")])
    assert (tmp_path / "result.json").read_text() == "existing"


def test_blocked_resume_keeps_original_attempt_and_usage_in_denominator(tmp_path):
    from app.evaluation.single_pass_journal import AttemptJournal
    from app.evaluation.single_pass_runner import blocked_case

    journal = AttemptJournal(tmp_path / "journal.sqlite")
    token = journal.reserve("run", "case-001", "fingerprint")
    journal.finish(
        "run",
        "case-001",
        token,
        {"status": "error", "model_calls": 1, "usage": {"total_tokens": 99}},
    )
    result = blocked_case(journal, "run", "case-001", "attempt_blocked")
    assert result["model_calls"] == 1 and result["usage"]["total_tokens"] == 99
    assert result["new_model_calls"] == 0
    assert blocked_case(journal, "run", "case-003", "invalid_packet")["model_calls"] == 0


def test_summary_counts_errors_unknowns_and_all_strata():
    from app.evaluation.single_pass_runner import summarize_runs

    runs = [
        {
            "status": "supported",
            "score": {"joint_correct": True, "service_correct": True, "fault_correct": True},
            "model_calls": 1,
            "usage": {"total_tokens": 100, "observed": True},
            "fault": "cpu",
            "ground_truth": {"fault": "cpu", "system": "ob"},
            "availability": {"logs": "present", "traces": "present"},
        },
        {
            "status": "error",
            "score": {"joint_correct": False, "service_correct": False, "fault_correct": False},
            "model_calls": 0,
            "ground_truth": {"fault": "loss", "system": "ss"},
            "availability": {"logs": "not_available", "traces": "not_available"},
        },
    ]
    summary = summarize_runs(runs)
    assert summary["case_count"] == 2 and summary["joint_correct"] == 1
    assert summary["joint_accuracy"] == 0.5
    assert 0 < summary["joint_wilson95"][0] < 0.5 < summary["joint_wilson95"][1] < 1
    assert summary["strata"]["fault"]["loss"]["case_count"] == 1
    assert summary["strata"]["traces"]["not_available"]["joint_correct"] == 0


def test_source_snapshot_freezes_actual_code_without_subprocess(tmp_path):
    from app.evaluation.single_pass_runner import source_snapshot

    (tmp_path / "one.py").write_text("x=1\n")
    before = source_snapshot(tmp_path)
    assert source_snapshot(tmp_path) == before
    (tmp_path / "one.py").write_text("x=2\n")
    assert source_snapshot(tmp_path) != before


def test_curated_profile_requires_explicit_manifest_not_pilot_fallback_name(tmp_path):
    root = tmp_path / "unidentified"
    root.mkdir()
    output = tmp_path / "output"
    with pytest.raises(SystemExit) as error:
        main(["--data-dir", str(root), "--output-dir", str(output)])
    assert error.value.code == 2
    assert not output.exists()


def test_unidentified_dataset_can_opt_out_of_curated_profile(tmp_path):
    root = tmp_path / "unidentified"
    root.mkdir()
    output = tmp_path / "output"
    result = main(
        ["--data-dir", str(root), "--output-dir", str(output), "--telemetry-profile", "unknown"]
    )
    assert result == 1  # Missing telemetry is reported without claiming RE2 provenance.
    import json

    report = json.loads((output / "validation.json").read_text(encoding="utf-8"))
    assert report["model_calls"] == 0
