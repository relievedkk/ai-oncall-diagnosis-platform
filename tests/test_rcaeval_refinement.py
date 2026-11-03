import json
import math

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


@pytest.fixture
def telemetry(tmp_path):
    def make(**columns):
        from app.evaluation.rcaeval import CaseView

        times = list(range(20))
        pd.DataFrame({"time": times, **columns}).to_parquet(tmp_path / "metrics.parquet")
        pd.DataFrame(
            {
                "timestamp": [0, 19],
                "container_name": ["orders", "orders"],
                "message": ["normal", "busy"],
            }
        ).to_parquet(tmp_path / "logs.parquet")
        (tmp_path / "inject_time.txt").write_text("5")
        return CaseView(tmp_path, "case-detail")

    return make


def test_missing_baseline_keeps_post_observations_without_claiming_fault(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_diskio=[None] * 5 + [20.0] * 15)
    profile = _profile_from_metrics(view, "orders", "disk")
    assert profile.available is True
    assert profile.coverage.status == "baseline_missing"
    assert profile.coverage.before_samples == 0
    assert profile.coverage.after_samples == 15
    assert profile.post_start_mean == 20
    assert profile.strong_change is False and profile.fault_eligible is False
    trend = view.metric_trends("orders", mechanisms=("disk",))[0]
    assert trend["baseline_median"] is None
    assert trend["comparison_available"] is False
    assert trend["after_samples"] == 15


def test_missing_post_is_coverage_gap_not_normal(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    profile = _profile_from_metrics(telemetry(orders_cpu=[1.0] * 5 + [None] * 15), "orders", "cpu")
    assert profile.coverage.status == "post_missing"
    assert profile.coverage.after_samples == 0
    assert not profile.strong_change and not profile.fault_eligible


@pytest.mark.parametrize(
    "mechanism,suffix",
    [
        ("cpu", "cpu"),
        ("mem", "mem"),
        ("delay", "latency-90"),
        ("disk", "diskio"),
        ("loss", "packet_loss"),
        ("socket", "socket"),
    ],
)
def test_trends_are_scoped_to_requested_mechanism(telemetry, mechanism, suffix):
    columns = {
        f"orders_{s}": [1.0] * 5 + [20.0] * 15
        for s in ("cpu", "mem", "latency-90", "diskio", "packet_loss", "socket")
    }
    rows = telemetry(**columns).metric_trends("orders", mechanisms=(mechanism,))
    assert [row["metric"] for row in rows] == [f"orders_{suffix}"]


def test_nonfinite_values_never_become_evidence_or_break_serialization(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_cpu=[1, 1, float("inf"), 1, float("nan")] + [20] * 15)
    profile = _profile_from_metrics(view, "orders", "cpu")
    assert profile.ratio is not None and math.isfinite(profile.ratio)
    rows = view.metric_trends("orders", mechanisms=("cpu",))
    json.dumps(rows, allow_nan=False)


def test_zero_baseline_and_constant_series_are_handled_explicitly(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_cpu=[0] * 5 + [10] * 15, orders_mem=[0] * 20)
    changed = _profile_from_metrics(view, "orders", "cpu")
    stable = _profile_from_metrics(view, "orders", "mem")
    assert changed.strong_change and changed.ratio is None
    assert not stable.strong_change


def test_trend_query_rejects_unsupported_mechanism(telemetry):
    with pytest.raises(ValueError):
        telemetry(orders_cpu=[1] * 20).metric_trends("orders", mechanisms=("shell",))


def _initial_and_request(view):
    from app.core.diagnostic_evidence import EvidenceRequest, InitialEvidenceBundle
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    initial = build_initial_evidence(view, max_bytes=16384)
    parsed = InitialEvidenceBundle.model_validate(initial.payload)
    candidate = parsed.candidates[0]
    request = EvidenceRequest(
        services=("orders",),
        mechanisms=("cpu", "mem"),
        view="mechanism_details",
        justification_refs=tuple(f.evidence_ref for f in candidate.facts),
    )
    return initial, parsed, request


def test_refinement_adds_stable_facts_and_independently_verified_resolution(telemetry):
    from app.core.diagnostic_evidence import DiagnosisEvidencePolicy
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 10 + [20] * 10)
    initial, parsed, request = _initial_and_request(view)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    again = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert follow.sha256 == again.sha256
    # The old minute-scale rule cannot separate these 5-second-separated onsets.
    assert "orders" in DiagnosisEvidencePolicy.from_bundles(initial).ambiguous_services
    unverified = DiagnosisEvidencePolicy.from_bundles(initial, follow)
    assert "orders" in unverified.ambiguous_services
    certificate = verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384)
    assert len(certificate) == 1 and certificate[0][:2] == ("orders", "cpu")
    verified = DiagnosisEvidencePolicy.from_bundles(
        initial, follow, verified_resolutions=certificate
    )
    assert "orders" not in verified.ambiguous_services
    assert certificate[0][2] in verified.mechanism_refs[("orders", "cpu")]
    assert follow.byte_length <= 16384


@pytest.mark.parametrize("offset", [5, 6])
def test_tied_or_overlapping_onsets_do_not_clear_ambiguity(telemetry, offset):
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * offset + [20] * (20 - offset))
    _, parsed, request = _initial_and_request(view)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384) == ()


def test_forged_temporal_certificate_is_rejected_against_raw_data(telemetry):
    from app.core.diagnostic_evidence import FollowupEvidenceBundle, serialize_evidence_bundle
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 10 + [20] * 10)
    _, parsed, request = _initial_and_request(view)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    payload = follow.payload
    payload["candidates"][0]["facts"][-1]["data"]["forged"] = True
    forged = serialize_evidence_bundle(
        FollowupEvidenceBundle.model_validate(payload), max_bytes=16384
    )
    with pytest.raises(ValueError):
        verify_refinement_evidence(view, request, forged, initial=parsed, max_bytes=16384)


def test_incomplete_competitor_request_cannot_certify_winner(telemetry):
    from app.core.diagnostic_evidence import EvidenceRequest
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 10 + [20] * 10)
    _, parsed, request = _initial_and_request(view)
    request = EvidenceRequest(**{**request.model_dump(), "mechanisms": ("cpu",)})
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384) == ()


def test_planner_produces_authorized_query_without_truth(telemetry):
    from app.core.diagnosis_result import DiagnosisResult
    from app.core.diagnostic_evidence import validate_evidence_request
    from app.evaluation.rcaeval_refinement import plan_refinement

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 10 + [20] * 10)
    _, parsed, _ = _initial_and_request(view)
    request = plan_refinement(parsed, DiagnosisResult())
    assert request.services == ("orders",)
    assert set(request.mechanisms) == {"cpu", "mem"}
    validate_evidence_request(request, parsed)


def test_verified_temporal_winner_revokes_initial_competing_authorization(telemetry):
    from app.core.diagnostic_evidence import DiagnosisEvidencePolicy
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15)
    view.m = pd.DataFrame(
        {
            "time": list(range(201)),
            "orders_cpu": [1] * 5 + [20] + [1] * 144 + [20] * 51,
            "orders_mem": [1] * 75 + [20] * 126,
        }
    )
    view.end = 200
    initial, parsed, request = _initial_and_request(view)
    assert ("orders", "cpu") in DiagnosisEvidencePolicy.from_bundles(initial).mechanism_refs
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    certificate = verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384)
    assert certificate[0][:2] == ("orders", "mem")
    assert follow.payload["candidates"][0]["mechanism_profiles"]["cpu"]["fault_eligible"] is False
    policy = DiagnosisEvidencePolicy.from_bundles(initial, follow, verified_resolutions=certificate)
    assert ("orders", "mem") in policy.mechanism_refs
    assert ("orders", "cpu") not in policy.mechanism_refs


def test_renamed_existing_observation_is_not_semantic_novelty():
    from app.core.diagnostic_evidence import FollowupEvidenceBundle, serialize_evidence_bundle
    from app.evaluation.rcaeval_refinement import refinement_has_novel_observations
    from tests.test_adaptive_diagnosis import followup
    from tests.test_two_stage_diagnosis import _initial

    payload = followup(novel=False).payload
    candidate = payload["candidates"][0]
    candidate["facts"][0]["evidence_ref"] = "renamed-ref"
    candidate["mechanism_profiles"]["cpu"]["evidence_refs"] = ["renamed-ref"]
    candidate["trends"] = [{"metric": "orders_cpu", "renamed": True}]
    renamed = serialize_evidence_bundle(
        FollowupEvidenceBundle.model_validate(payload), max_bytes=16384
    )
    assert not refinement_has_novel_observations(_initial(), renamed)


def test_subset_of_existing_trends_has_no_new_observations(telemetry):
    from app.core.diagnostic_evidence import EvidenceRequest
    from app.evaluation.rcaeval_evidence import build_followup_evidence
    from app.evaluation.rcaeval_refinement import refinement_has_novel_observations

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 10 + [20] * 10)
    initial, parsed, req = _initial_and_request(view)
    req = EvidenceRequest(**{**req.model_dump(), "mechanisms": ("cpu",)})
    follow = build_followup_evidence(view, req, initial=parsed, max_bytes=16384)
    assert not refinement_has_novel_observations(initial, follow)


def test_planner_can_expand_one_authorized_hint_when_root_is_unknown():
    from app.core.diagnosis_result import DiagnosisResult
    from app.core.diagnostic_evidence import InitialEvidenceBundle, validate_evidence_request
    from app.evaluation.rcaeval_refinement import plan_refinement
    from tests.test_two_stage_diagnosis import _initial

    parsed = InitialEvidenceBundle.model_validate(_initial().payload)
    request = plan_refinement(parsed, DiagnosisResult())
    assert set(request.services) == {"orders", "payments"}
    validate_evidence_request(request, parsed)


@pytest.mark.parametrize("partial", [float("nan"), float("inf")])
def test_missing_or_nonfinite_competitor_cannot_certify_resolution(telemetry, partial):
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[1] * 5 + [partial] * 13 + [20] * 2)
    _, parsed, request = _initial_and_request(view)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384) == ()


def test_common_mode_signal_cannot_certify_root_mechanism(telemetry):
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        verify_refinement_evidence,
    )

    columns = {
        f"{s}_{m}": [1] * onset + [20] * (20 - onset)
        for s in ("orders", "peer-a", "peer-b")
        for m, onset in (("cpu", 5), ("mem", 10))
    }
    view = telemetry(**columns)
    _, parsed, request = _initial_and_request(view)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert verify_refinement_evidence(view, request, follow, initial=parsed, max_bytes=16384) == ()


def test_restored_partial_coverage_detail_is_useful_without_inventing_cause(telemetry):
    from app.core.diagnostic_evidence import InitialEvidenceBundle, serialize_evidence_bundle
    from app.evaluation.rcaeval_refinement import (
        build_refinement_evidence,
        refinement_has_novel_observations,
    )

    view = telemetry(orders_cpu=[1] * 5 + [20] * 15, orders_mem=[None] * 5 + [30] * 15)
    initial, _, request = _initial_and_request(view)
    payload = initial.payload
    payload["candidates"][0]["trends"] = []
    payload["omitted_paths"] = ["candidates[0].trends"]
    parsed = InitialEvidenceBundle.model_validate(payload)
    compact = serialize_evidence_bundle(parsed, max_bytes=16384)
    follow = build_refinement_evidence(view, request, initial=parsed, max_bytes=16384)
    assert refinement_has_novel_observations(compact, follow)
    mem = follow.payload["candidates"][0]["mechanism_profiles"]["mem"]
    assert mem["coverage"]["after_samples"] == 15
    assert mem["fault_eligible"] is False
