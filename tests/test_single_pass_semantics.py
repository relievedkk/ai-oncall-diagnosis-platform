import pytest

from app.evaluation.single_pass_hypotheses import collect_evidence, rank_hypotheses
from tests.single_pass_helpers import view_of


def cpu_view():
    columns = {
        "a_" + family: [1.0] * 10 + [1.0] * 21
        for family in ("cpu", "mem", "socket", "diskio", "workload", "latency-90")
    }
    columns["a_cpu"] = [1.0] * 10 + [20.0] * 21
    columns["a_latency-90"] = [1.0] * 10 + [4.0] * 21
    return view_of(columns)


def test_published_cpu_quantity_allows_relative_judgment_without_inventing_units():
    view = cpu_view()
    plain = collect_evidence(view)
    profiled = collect_evidence(view, telemetry_profile="rcaeval-re2")
    assert not next(h for h in rank_hypotheses(plain) if h.fault == "cpu").eligible
    cpu = next(f for f in profiled.facts if f.family == "cpu")
    assert cpu.data["unit"] == "unknown" and cpu.data["metric_kind"] == "unknown"
    assert cpu.data["quantity"] == "cpu_usage"
    assert cpu.data["interpretation"] == "relative_change_only"
    assert cpu.data["semantic_source"]
    assert next(h for h in rank_hypotheses(profiled) if h.fault == "cpu").eligible
    assert cpu.ref != next(f for f in plain.facts if f.family == "cpu").ref


def test_profile_does_not_bless_cpu_named_counters_or_unrecognized_fields():
    view = view_of({"a_cpu_seconds_total": [1.0] * 10 + [20.0] * 10})
    e = collect_evidence(view, telemetry_profile="rcaeval-re2")
    cpu = next(f for f in e.facts if f.family == "cpu")
    assert cpu.data.get("quantity", "unknown") == "unknown"
    assert cpu.data["unit"] == "unknown" and cpu.data["metric_kind"] == "unknown"
    assert not any(h.eligible for h in rank_hypotheses(e))


def test_metadata_cannot_override_changed_workload_or_competing_resources():
    view = cpu_view()
    view.m["a_workload"] = [1.0] * 10 + [20.0] * 21
    e = collect_evidence(view, telemetry_profile="rcaeval-re2")
    assert not next(h for h in rank_hypotheses(e) if h.fault == "cpu").eligible


def test_only_documented_dashboard_series_gets_verified_latency_query_units():
    view = view_of({"ts-ui-dashboard_latency-90": [1.0] * 20, "other_latency-90": [1.0] * 20})
    e = collect_evidence(view, telemetry_profile="rcaeval-re2")
    dashboard = next(f for f in e.facts if f.service == "ts-ui-dashboard")
    other = next(f for f in e.facts if f.service == "other")
    assert dashboard.data["unit"] == "seconds"
    assert dashboard.data["lookback_seconds"] == 60
    assert other.data["unit"] == "unknown"


def test_unknown_profile_is_rejected_without_loading_or_changing_telemetry():
    with pytest.raises(ValueError, match="unknown_telemetry_profile"):
        collect_evidence(cpu_view(), telemetry_profile="invented")


def test_explicit_counter_with_unknown_unit_cannot_acquire_cpu_authority():
    from itertools import accumulate

    view = cpu_view()
    view.m["a_cpu"] = list(accumulate([1.0] * 10 + [20.0] * 21))
    e = collect_evidence(view, semantics={"a_cpu": {"kind": "counter"}})
    assert next(f for f in e.facts if f.family == "cpu").data["unit"] == "unknown"
    assert not next(h for h in rank_hypotheses(e) if h.fault == "cpu").eligible


def test_profile_reaches_base_and_all_time_perturbations_before_truth(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.evaluation import single_pass_runner as runner
    from app.evaluation.rcaeval_v4 import DevelopmentScope

    answers = tmp_path / "answers.parquet"
    answers.touch()
    view = cpu_view()
    view.case_id = "case-013"
    profiles = []
    original = runner.collect_evidence

    def collect(*args, **kwargs):
        profiles.append(kwargs.get("telemetry_profile"))
        return original(*args, **kwargs)

    def truth(_):
        assert profiles == ["rcaeval-re2"] * 5
        return {"root_cause_service": "a", "fault": "cpu"}

    monkeypatch.setattr(runner, "collect_evidence", collect)
    pilot = SimpleNamespace(
        ids={"case-013": "ignored"}, answers_file=answers, open=lambda _: view, truth=truth
    )
    audit = runner.audit_development(
        pilot, scope=DevelopmentScope(answers), telemetry_profile="rcaeval-re2"
    )
    assert audit["audited"] == 1 and audit["eligible_correct_pairs"] == 1


def test_cpu_usage_permission_handles_credible_subthreshold_connection_competition():
    view = cpu_view()
    view.m["a_socket"] = [20.0] * 10 + [35.0] * 6 + [28.0] * 15
    e = collect_evidence(view, telemetry_profile="rcaeval-re2")
    socket = next(f for f in e.facts if f.family == "socket")
    assert socket.data["changed"] is False
    cpu = next(h for h in rank_hypotheses(e) if h.fault == "cpu")
    assert not cpu.eligible
    assert socket.ref in cpu.opposing_refs
    assert "unresolved_competing_explanation" in cpu.gaps
