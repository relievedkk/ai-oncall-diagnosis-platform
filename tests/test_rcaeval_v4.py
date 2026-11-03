import pytest

from app.evaluation.rcaeval_models import EvaluationDiagnosisResult
from tests.test_two_stage_diagnosis import FakeModel, _factory


@pytest.mark.asyncio
async def test_v4_freezes_both_results_before_truth_and_preserves_parity(telemetry):
    from app.evaluation.rcaeval_v4 import run_protocol_v4_case

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15)
    models = {
        "single_stage": FakeModel([EvaluationDiagnosisResult()]),
        "agent": FakeModel([EvaluationDiagnosisResult(), EvaluationDiagnosisResult()]),
    }
    reads = []

    def truth(case_id):
        assert len(models["single_stage"].calls) == 1
        assert len(models["agent"].calls) == 2
        reads.append(case_id)
        return {"root_cause_service": "orders", "fault": "cpu"}

    result = await run_protocol_v4_case(
        view,
        view.public_task(),
        model_factory_for=lambda s: _factory(models[s]),
        truth_loader=truth,
        timeout_seconds=3,
    )
    assert reads == ["case-detail"]
    single, agent = result["runs"]
    assert single["initial_hash"] == agent["initial_hash"]
    assert all(r["protocol_version"] == 4 and r["scoring_version"] == 4 for r in result["runs"])
    assert agent["usage"]["model_calls"] == 2
    assert agent["refinement_status"] == "rejected"
    assert agent["enrichment_failure_detail_code"] is None
    assert agent["schema_issues"] == []
    assert "scoring_v4" in agent and "scoring_v3" not in agent


def _run(strategy, service, fault):
    return {
        "protocol_version": 4,
        "strategy": strategy,
        "case_id": "case-x",
        "prediction": {"service": service, "fault": fault},
        "ground_truth": {"root_cause_service": "orders", "fault": "cpu"},
        "diagnosis_result": {"outcome": "insufficient_evidence"},
        "data_availability": {"traces": "not_available"},
        "usage": {"model_calls": 1},
    }


def test_v4_metrics_report_paired_gains_losses_and_strata():
    from app.evaluation.rcaeval_v4 import summarize_v4

    summary = summarize_v4([_run("single_stage", "", "unknown"), _run("agent", "orders", "cpu")])
    assert summary["paired"]["new_joint_hits"] == ["case-x"]
    assert summary["paired"]["lost_joint_hits"] == []
    assert summary["metrics"]["agent"]["joint_accuracy"] == 1
    assert summary["strata"]["fault"]["cpu"]["agent"]["runs"] == 1
    assert summary["strata"]["traces"]["not_available"]["single_stage"]["runs"] == 1


def test_v4_does_not_pool_revised_or_historical_v3_records():
    from app.evaluation.rcaeval_v4 import summarize_v4

    old = {**_run("agent", "orders", "cpu"), "protocol_version": 3}
    with pytest.raises(ValueError):
        summarize_v4([old])


def test_runner_refuses_nonempty_output_before_opening_dataset(tmp_path):
    from app.evaluation.rcaeval import main

    (tmp_path / "summary.json").write_text("keep this result")
    with pytest.raises(SystemExit) as caught:
        main(["--output-dir", str(tmp_path), "--data-dir", str(tmp_path / "absent")])
    assert caught.value.code == 2
    assert (tmp_path / "summary.json").read_text() == "keep this result"


def test_development_audit_is_model_free_and_bound_to_small_development_set(telemetry):
    from app.evaluation.rcaeval_v4 import audit_development

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15)

    class Development:
        ids = {"case-detail": "case-detail"}

        def open(self, case_id):
            return view

        def truth(self, case_id):
            return {"root_cause_service": "orders", "fault": "cpu"}

    audit = audit_development(Development(), max_bytes=16384)
    assert audit["model_calls"] == 0
    assert audit["initial_candidate_recall"] == 1
    assert audit["initial_eligible_correct_mechanisms"] == 1
    assert audit["cases"][0]["truth_mechanism_state"] == "direct_signal"
    oversized = Development()
    oversized.ids = {str(i): str(i) for i in range(19)}
    with pytest.raises(ValueError):
        audit_development(oversized, max_bytes=16384)
