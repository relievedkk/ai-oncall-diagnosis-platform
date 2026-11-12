import pandas as pd
import pytest

from tests.test_heldout_protocol import dataset, isolated_registry  # noqa: F401


def test_model_metrics_keep_errors_and_refusals_in_denominator():
    from app.evaluation.heldout_score import summarize_models

    rows = [
        {
            "truth": {"service": "orders", "fault": "cpu"},
            "prediction": {
                "status": "supported",
                "service": "orders",
                "fault": "cpu",
                "confirmed": True,
                "proposal": {"service": "orders", "fault": "cpu"},
                "model_calls": 1,
            },
        },
        {
            "truth": {"service": "orders", "fault": "mem"},
            "prediction": {
                "status": "candidate",
                "service": "orders",
                "fault": "unknown",
                "candidate_service": "orders",
                "candidate_fault": "mem",
                "proposal": {"service": "orders", "fault": "mem"},
                "model_calls": 1,
            },
        },
        {
            "truth": {"service": "orders", "fault": "loss"},
            "prediction": {"status": "not_run", "proposal": {"service": "orders", "fault": "loss"}},
        },
        {
            "truth": {"service": "orders", "fault": "disk"},
            "prediction": {
                "status": "insufficient_evidence",
                "proposal": {"service": None, "fault": "unknown"},
                "model_calls": 1,
            },
        },
    ]
    result = summarize_models(rows)
    assert result["n"] == 4
    assert result["proposal_joint_accuracy"] == 0.5
    assert result["proposal_fault_accuracy"] == 0.5
    assert result["candidate_joint_accuracy"] == 0.5
    assert result["confirmed_joint_accuracy"] == 0.25
    assert result["confirmation_coverage"] == 0.25
    assert result["not_run"] == 1 and result["insufficient_evidence"] == 1
    assert result["observed_tokens"] is None
    assert result["model_calls"] == 3


def test_wilson_interval_is_hand_checked_and_empty_denominator_is_null():
    from app.evaluation.heldout_score import wilson

    assert wilson(0, 0) is None
    low, high = wilson(1, 2)
    assert low == pytest.approx(0.094531, abs=0.00001)
    assert high == pytest.approx(0.905469, abs=0.00001)


def test_answers_require_exact_unique_case_set_and_known_faults():
    from app.evaluation.heldout_score import validate_answers

    row = {"case_id": "case-001", "root_cause_service": "orders", "fault": "cpu", "system": "ob"}
    with pytest.raises(ValueError, match="answer_scope_mismatch"):
        validate_answers(pd.DataFrame([row, row]), ["case-001"])
    with pytest.raises(ValueError, match="answer_scope_mismatch"):
        validate_answers(pd.DataFrame([row]), ["case-001", "case-002"])
    with pytest.raises(ValueError, match="invalid_answer"):
        validate_answers(pd.DataFrame([{**row, "fault": "made_up"}]), ["case-001"])


def test_offline_scoring_is_explicit_and_does_not_claim_model_accuracy(tmp_path):
    from app.evaluation.heldout_protocol import prepare_run
    from app.evaluation.heldout_score import score_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    with pytest.raises(ValueError, match="unfinalized"):
        score_run(tmp_path / "run", data["answers_file"])
    report = score_run(tmp_path / "run", data["answers_file"], offline_only=True)
    assert report["model"] is None
    assert report["baseline"]["joint_top1_accuracy"] == 1
    assert report["baseline"]["joint_top5_recall"] == 1
    assert report["baseline"]["service_top3_recall"] == 1
    assert report["retired"] is True
    assert score_run(tmp_path / "run", data["answers_file"], offline_only=True) == report


def test_modified_prediction_rejected_before_answer_read(tmp_path, monkeypatch):
    from app.evaluation.heldout_protocol import finalize_run, prepare_run
    from app.evaluation.heldout_score import score_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    finalize_run(tmp_path / "run", mode="offline-only")
    (tmp_path / "run/case-001-baseline.json").write_text("{}", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("answers read before frozen artifacts validated")

    monkeypatch.setattr(pd, "read_parquet", forbidden)
    with pytest.raises(ValueError, match="artifact_changed"):
        score_run(tmp_path / "run", data["answers_file"], offline_only=True)


def test_changed_answer_file_is_not_accepted_as_a_new_scoring_target(tmp_path):
    from app.evaluation.heldout_protocol import prepare_run
    from app.evaluation.heldout_score import score_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    answer = pd.read_parquet(data["answers_file"])
    answer["fault"] = "mem"
    answer.to_parquet(data["answers_file"])
    with pytest.raises(ValueError, match="answers_commitment_mismatch"):
        score_run(tmp_path / "run", data["answers_file"], offline_only=True)


def test_score_cli_explicitly_retires_offline_dataset(tmp_path):
    from app.evaluation.heldout_cli import main
    from app.evaluation.heldout_protocol import prepare_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    assert (
        main(
            [
                "score",
                "--run",
                str(tmp_path / "run"),
                "--answers",
                data["answers_file"],
                "--offline-only",
            ]
        )
        == 0
    )
    assert (tmp_path / "run/scores.json").exists()
