import pytest

from app.evaluation.rcaeval_models import EvaluationDiagnosisResult
from tests.test_rcaeval_refinement import telemetry as telemetry
from tests.test_two_stage_diagnosis import FakeModel, _factory


@pytest.mark.asyncio
async def test_v4_freezes_both_results_before_truth_and_preserves_parity(telemetry):
    from app.evaluation.rcaeval_v4 import run_protocol_v4_case

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_diskio=[None] * 5 + [10] * 15)
    models = {
        "single_stage": FakeModel([EvaluationDiagnosisResult()]),
        "agent": FakeModel([EvaluationDiagnosisResult(), EvaluationDiagnosisResult()]),
    }
    reads = []

    async def truth(case_id):
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
    assert agent["evidence_coverage"]["orders"]["cpu"]["state"] == "direct_signal"
    assert agent["evidence_coverage"]["orders"]["loss"]["comparison_status"] == "unavailable"
    assert agent["evidence_coverage"]["orders"]["disk"]["comparison_status"] == "baseline_missing"
    assert agent["evidence_coverage"]["orders"]["disk"]["partial_samples"]["after_samples"] == 15


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


def test_v4_rejects_mixed_revisions_and_duplicate_case_records():
    from app.evaluation.rcaeval_v4 import summarize_v4

    first = _run("agent", "orders", "cpu")
    with pytest.raises(ValueError):
        summarize_v4([first, {**first, "prompt_revision": "different"}])
    with pytest.raises(ValueError):
        summarize_v4([first, first])


def test_paired_summary_exposes_lost_baseline_hits():
    from app.evaluation.rcaeval_v4 import summarize_v4

    result = summarize_v4([_run("single_stage", "orders", "cpu"), _run("agent", "", "unknown")])
    assert result["paired"]["lost_joint_hits"] == ["case-x"]
    assert result["paired"]["lost_service_hits"] == ["case-x"]


def test_runner_refuses_nonempty_output_before_opening_dataset(tmp_path):
    from app.evaluation.rcaeval import main

    (tmp_path / "summary.json").write_text("keep this result")
    with pytest.raises(SystemExit) as caught:
        main(["--output-dir", str(tmp_path), "--data-dir", str(tmp_path / "absent")])
    assert caught.value.code == 2
    assert (tmp_path / "summary.json").read_text() == "keep this result"


def test_development_audit_is_model_free_and_bound_to_small_development_set(telemetry, tmp_path):
    from app.evaluation.rcaeval_v4 import DevelopmentScope, audit_development

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15)
    view.case_id = "case-001"
    approved = tmp_path / "approved-answers.parquet"
    approved.write_text("test capability binding")
    scope = DevelopmentScope(answers_file=approved)

    class Development:
        ids = {"case-001": "case-001"}
        answers_file = approved

        def open(self, case_id):
            return view

        def truth(self, case_id):
            return {"root_cause_service": "orders", "fault": "cpu"}

    audit = audit_development(Development(), scope=scope, max_bytes=16384)
    assert audit["model_calls"] == 0
    assert audit["initial_candidate_recall"] == 1
    assert audit["initial_eligible_correct_mechanisms"] == 1
    assert audit["cases"][0]["truth_mechanism_state"] == "direct_signal"
    oversized = Development()
    oversized.ids = {str(i): str(i) for i in range(19)}
    with pytest.raises(ValueError):
        audit_development(oversized, scope=scope, max_bytes=16384)


def test_development_scope_rejects_sealed_subset_before_any_data_or_truth_access(tmp_path):
    from app.evaluation.rcaeval_v4 import DevelopmentScope, audit_development

    approved = tmp_path / "approved.parquet"
    approved.write_text("scope")
    scope = DevelopmentScope(answers_file=approved)

    class Sealed:
        ids = {"case-002": "sealed"}
        answers_file = approved

        def open(self, case_id):
            raise AssertionError("sealed telemetry opened")

        def truth(self, case_id):
            raise AssertionError("sealed answers opened")

    with pytest.raises(ValueError):
        audit_development(Sealed(), scope=scope)
    Sealed.ids = {"case-001": "case-001"}
    Sealed.answers_file = tmp_path / "unapproved.parquet"
    with pytest.raises(ValueError):
        audit_development(Sealed(), scope=scope)


@pytest.mark.asyncio
async def test_absent_logs_and_traces_remain_explicit_without_crashing(telemetry):
    from app.evaluation.rcaeval import validate
    from app.evaluation.rcaeval_v4 import run_protocol_v4_case

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15)
    view.l = view.l.iloc[:0].copy()
    models = {
        "single_stage": FakeModel([EvaluationDiagnosisResult()]),
        "agent": FakeModel([EvaluationDiagnosisResult(), EvaluationDiagnosisResult()]),
    }
    pair = await run_protocol_v4_case(
        view,
        view.public_task(),
        model_factory_for=lambda s: _factory(models[s]),
        truth_loader=lambda _: {"root_cause_service": "orders", "fault": "cpu"},
        timeout_seconds=3,
    )
    assert pair["runs"][0]["data_availability"] == {
        "traces": "not_available",
        "logs": "not_available",
    }

    class Pilot:
        ids = {"case-detail": "case-detail"}

        def open(self, _):
            return view

    assert validate(Pilot())["passed"] is True


def test_missing_log_file_is_explicitly_unavailable(telemetry, tmp_path):
    from app.evaluation.rcaeval import CaseView

    telemetry(orders_cpu=[1] * 5 + [20] * 15)
    (tmp_path / "logs.parquet").unlink()
    view = CaseView(tmp_path, "case-no-logs")
    assert view.logs("orders")["data_status"] == "not_available"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,suffix",
    [
        ("cpu", "cpu"),
        ("mem", "mem"),
        ("delay", "latency-90"),
        ("disk", "diskio"),
        ("loss", "packet_loss"),
        ("socket", "socket"),
    ],
)
async def test_each_fault_can_complete_real_policy_without_unnecessary_refinement(
    telemetry, fault, suffix
):
    from app.evaluation.rcaeval_evidence import build_initial_evidence
    from app.evaluation.rcaeval_v4 import run_protocol_v4_case

    view = telemetry(**{f"orders_{suffix}": [1] * 5 + [20] * 15})
    initial = build_initial_evidence(view, max_bytes=16384)
    ref = initial.payload["candidates"][0]["mechanism_profiles"][fault]["evidence_refs"][0]
    hypothesis = {
        "description": "same-service direct incident change",
        "service": "orders",
        "fault_type": fault,
        "supporting_refs": [ref],
        "status": "supported",
    }
    diagnosis = EvaluationDiagnosisResult(
        outcome="diagnosed",
        root_cause_service="orders",
        fault_type=fault,
        cause_evidence_refs=[ref],
        diagnosis_complete=True,
        observations=[{"text": "incident shift", "evidence_refs": [ref]}],
        hypotheses=[hypothesis],
        candidate_comparisons=[hypothesis],
    )
    models = {s: FakeModel([diagnosis]) for s in ("single_stage", "agent")}
    result = await run_protocol_v4_case(
        view,
        view.public_task(),
        model_factory_for=lambda s: _factory(models[s]),
        truth_loader=lambda _: {"root_cause_service": "orders", "fault": fault},
        timeout_seconds=3,
    )
    assert all(r["scoring_v4"]["joint_correct"] for r in result["runs"])
    assert result["runs"][1]["refinement_status"] == "skipped_complete"
    assert result["runs"][1]["usage"]["model_calls"] == 1
