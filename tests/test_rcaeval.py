import json
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


@pytest.fixture
def recorded_case(tmp_path):
    import pandas as pd

    from app.evaluation.rcaeval import CaseView

    pd.DataFrame({"time": [100, 101, 102], "orders_cpu": [1.0, 2.0, 90.0]}).to_parquet(
        tmp_path / "metrics.parquet"
    )
    pd.DataFrame(
        {
            "timestamp": [100, 102],
            "container_name": ["orders", "orders"],
            "message": ["normal", "busy"],
        }
    ).to_parquet(tmp_path / "logs.parquet")
    pd.DataFrame(
        {
            "startTimeMillis": [100000, 102000],
            "time": ["wrong", "wrong"],
            "serviceName": ["orders", "orders"],
            "duration": [10, 100],
        }
    ).to_parquet(tmp_path / "traces.parquet")
    (tmp_path / "inject_time.txt").write_text("101")
    return CaseView(tmp_path, "case-001")


def test_offline_validation_never_loads_sealed_truth(recorded_case):
    from app.evaluation.rcaeval import validate

    class SealedPilot:
        ids = {"case-001": "case-001"}

        @staticmethod
        def open(case_id):
            assert case_id == "case-001"
            return recorded_case

        @staticmethod
        def truth(case_id):
            raise AssertionError(f"sealed truth was opened for {case_id}")

    result = validate(
        SealedPilot(),
        development_truth_loader=lambda case_id: {
            "case_id": case_id,
            "root_cause_service": "orders",
        },
    )

    assert result["passed"] is True
    assert result["development_candidate_recall"] == {
        "cases": 1,
        "initial": 1,
        "initial_or_hinted": 1,
    }


def test_absolute_trace_time_and_service_filter(recorded_case):
    view = recorded_case
    assert view.traces()["services"][0]["after_p90_us"] == 100
    assert view.logs(service_name="orders", query="busy")["total"] == 1
    assert view.metrics()["changes"][0]["before_mean"] == 1
    with pytest.raises(ValueError):
        view.traces(start=float("nan"))
    with pytest.raises(ValueError):
        view.logs(limit=True)


def test_trace_single_window_returns_observations_without_fake_comparison(recorded_case):
    pre = recorded_case.traces(end=100)
    assert pre["data_status"] == "partial_comparison"
    assert pre["data_points"][0]["value"] == 10
    assert pre["services"][0]["after_p90_us"] is None
    assert pre["services"][0]["before_spans"] == 1
    empty = recorded_case.traces(start=101, end=101)
    assert empty["data_status"] == "no_data"
    assert empty["data_points"] == []
    assert empty["comparison_available"] is False


def test_case_view_marks_missing_trace_source_as_not_available(tmp_path):
    import pandas as pd

    from app.evaluation.rcaeval import CaseView

    pd.DataFrame({"time": [100, 101, 102], "orders_cpu": [1.0, 2.0, 3.0]}).to_parquet(
        tmp_path / "metrics.parquet"
    )
    pd.DataFrame(
        {
            "timestamp": [100, 102],
            "container_name": ["orders", "orders"],
            "message": ["normal", "busy"],
        }
    ).to_parquet(tmp_path / "logs.parquet")
    (tmp_path / "inject_time.txt").write_text("101")

    view = CaseView(tmp_path, "case-001")

    assert view.has_traces is False
    assert view.traces()["data_status"] == "not_available"
    assert view.call_edges()["reason"] == "trace telemetry not available"


def test_planner_sees_tool_schema(recorded_case):
    from app.agent.aiops.utils import format_tools_description

    description = format_tools_description(recorded_case.tools())
    assert "service_name" in description
    assert "start" in description and "end" in description
    assert "parameters" in description


def test_overview_and_candidate_bundle_are_label_free(recorded_case):
    overview = recorded_case.overview()
    assert overview["candidates"][0]["service"] == "orders"
    assert overview["metric_trends"]
    assert "root_cause" not in str(overview)
    bundle = recorded_case.candidate_evidence(["orders"])
    assert bundle["candidates"][0]["service"] == "orders"
    assert bundle["candidates"][0]["mechanism_observations"]["cpu"]
    with pytest.raises(ValueError):
        recorded_case.candidate_evidence(["orders", "orders"])


def _replace_metrics(recorded_case, **columns):
    import pandas as pd

    recorded_case.m = pd.DataFrame({"time": [0, 60, 120, 180, 240, 300, 360], **columns})
    recorded_case.start = 0
    recorded_case.incident = 180
    recorded_case.end = 360
    recorded_case.services = ["orders"]


def test_mechanism_profiles_separate_cpu_shift_from_stable_resources(recorded_case):
    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 20, 20, 20, 20],
        orders_mem=[10, 10, 10, 10, 10, 10, 10],
        **{"orders_latency-90": [1, 1, 1, 1, 1, 1, 1]},
    )
    profiles = recorded_case.mechanism_profiles("orders")
    assert profiles["cpu"]["strong_change"] is True
    assert profiles["cpu"]["post_pattern"] == "sustained_shift"
    assert profiles["mem"]["strong_change"] is False
    assert profiles["disk"]["available"] is False


def test_mechanism_profiles_keep_memory_growth_when_latency_is_larger(recorded_case):
    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 1, 1, 1, 1],
        orders_mem=[10, 10, 10, 20, 40, 80, 160],
        **{"orders_latency-90": [1, 1, 1, 20, 20, 20, 20]},
    )
    profiles = recorded_case.mechanism_profiles("orders")
    assert profiles["mem"]["strong_change"] is True
    assert profiles["mem"]["post_pattern"] == "growth"
    assert profiles["delay"]["interpretation"] == "may_be_symptom_with_resource_anomaly"


def test_mechanism_profiles_identify_delay_when_resources_are_stable(recorded_case):
    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 1, 1, 1, 1],
        orders_mem=[10, 10, 10, 10, 10, 10, 10],
        **{"orders_latency-90": [1, 1, 1, 20, 20, 20, 20]},
    )
    profiles = recorded_case.mechanism_profiles("orders")
    assert profiles["delay"]["strong_change"] is True
    assert profiles["delay"]["interpretation"] == "possible_direct_delay"


def test_loss_requires_structured_network_signal(recorded_case):
    from app.evaluation.rcaeval_evidence import build_mechanism_profiles

    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 1, 1, 1, 1],
        orders_packet_loss=[0, 0, 0, 8, 9, 10, 11],
    )
    profiles = build_mechanism_profiles(recorded_case, "orders")

    assert profiles["loss"].state == "direct_signal"
    assert profiles["loss"].fault_eligible is True
    assert profiles["loss"].strongest_metric == "orders_packet_loss"


def test_loss_accepts_structured_trace_network_error_signal(recorded_case):
    import pandas as pd

    from app.evaluation.rcaeval_evidence import build_mechanism_profiles

    _replace_metrics(recorded_case, orders_cpu=[1, 1, 1, 1, 1, 1, 1])
    recorded_case.t = pd.DataFrame(
        {
            "_epoch": [0, 60, 120, 180, 240, 300, 360],
            "serviceName": ["orders"] * 7,
            "network_error_count": [0, 0, 0, 8, 9, 10, 11],
        }
    )

    profiles = build_mechanism_profiles(recorded_case, "orders")

    assert profiles["loss"].state == "direct_signal"
    assert profiles["loss"].fault_eligible is True
    assert profiles["loss"].strongest_metric == "trace.network_error_count"


def test_log_text_cannot_promote_loss_to_direct_signal(recorded_case):
    import pandas as pd

    from app.evaluation.rcaeval_evidence import build_mechanism_profiles

    _replace_metrics(recorded_case, orders_cpu=[1, 1, 1, 1, 1, 1, 1])
    recorded_case.l = pd.DataFrame(
        {
            "timestamp": [181, 182, 183],
            "container_name": ["orders"] * 3,
            "message": ["packet loss timeout retransmit"] * 3,
        }
    )
    profiles = build_mechanism_profiles(recorded_case, "orders")

    assert profiles["loss"].state == "unavailable"
    assert profiles["loss"].fault_eligible is False
    assert profiles["loss"].evidence_refs == ()


def test_multiple_direct_signals_are_ambiguous_without_temporal_separation(recorded_case):
    from app.evaluation.rcaeval_evidence import (
        build_mechanism_profiles,
        classify_competing_mechanisms,
    )

    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 20, 20, 20, 20],
        orders_mem=[10, 10, 10, 200, 200, 200, 200],
        orders_diskio=[float("nan"), 1, 1, 1, 1, 1, 1],
    )
    profiles = classify_competing_mechanisms(
        build_mechanism_profiles(recorded_case, "orders"), common_mode=set()
    )

    assert profiles["cpu"].state == "direct_signal"
    assert profiles["mem"].state == "direct_signal"
    assert profiles["cpu"].fault_eligible is False
    assert profiles["mem"].fault_eligible is False
    assert profiles["cpu"].competing_mechanisms == ("mem",)


def test_earlier_persistent_unique_signal_can_disambiguate(recorded_case):
    from app.evaluation.rcaeval_evidence import (
        build_mechanism_profiles,
        classify_competing_mechanisms,
    )

    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 20, 20, 20, 20],
        orders_mem=[10, 10, 10, 10, 200, 200, 200],
    )
    profiles = classify_competing_mechanisms(
        build_mechanism_profiles(recorded_case, "orders"),
        common_mode=set(),
        bucket_seconds=60,
    )

    assert profiles["cpu"].first_anomalous_time == 180
    assert profiles["mem"].first_anomalous_time == 240
    assert profiles["cpu"].fault_eligible is True
    assert profiles["mem"].fault_eligible is False


def test_delay_is_symptom_when_resource_signal_is_direct(recorded_case):
    from app.evaluation.rcaeval_evidence import (
        build_mechanism_profiles,
        classify_competing_mechanisms,
    )

    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 20, 20, 20, 20],
        **{"orders_latency-90": [1, 1, 1, 30, 30, 30, 30]},
    )
    profiles = classify_competing_mechanisms(
        build_mechanism_profiles(recorded_case, "orders"), common_mode=set()
    )

    assert profiles["cpu"].state == "direct_signal"
    assert profiles["delay"].state == "possible_symptom"
    assert profiles["delay"].fault_eligible is False


@pytest.mark.parametrize(
    "common_mode,delay_precedes_callers,downstream_direct,eligible",
    [
        ({"cpu"}, True, False, True),
        (set(), True, False, False),
        ({"cpu"}, False, False, False),
        ({"cpu"}, True, True, False),
    ],
)
def test_delay_requires_all_three_rebuttal_conditions_to_be_direct(
    recorded_case,
    common_mode,
    delay_precedes_callers,
    downstream_direct,
    eligible,
):
    from app.evaluation.rcaeval_evidence import (
        build_mechanism_profiles,
        classify_competing_mechanisms,
    )

    _replace_metrics(
        recorded_case,
        orders_cpu=[1, 1, 1, 20, 20, 20, 20],
        **{"orders_latency-90": [1, 1, 1, 30, 30, 30, 30]},
    )
    profiles = classify_competing_mechanisms(
        build_mechanism_profiles(recorded_case, "orders"),
        common_mode=common_mode,
        delay_precedes_callers=delay_precedes_callers,
        downstream_direct_signal=downstream_direct,
    )

    assert profiles["delay"].fault_eligible is eligible
    assert profiles["delay"].state == ("direct_signal" if eligible else "possible_symptom")


def test_candidate_selection_is_mechanism_diverse_not_top_ratio_only(recorded_case):
    from app.evaluation.rcaeval_evidence import select_candidate_services

    _replace_metrics(
        recorded_case,
        alpha_cpu=[1, 1, 1, 1000, 1000, 1000, 1000],
        beta_cpu=[1, 1, 1, 900, 900, 900, 900],
        gamma_mem=[1, 1, 1, 8, 8, 8, 8],
        delta_latency=[1, 1, 1, 6, 6, 6, 6],
    )
    recorded_case.services = ["alpha", "beta", "delta", "gamma"]

    selection = select_candidate_services(recorded_case, max_candidates=3)

    assert selection.candidates == ("alpha", "delta", "gamma")
    assert "beta" not in selection.candidates
    assert "direct:cpu" in selection.reasons["alpha"]
    assert "direct:mem" in selection.reasons["gamma"]


def test_common_mode_disk_does_not_fill_candidate_budget(recorded_case):
    from app.evaluation.rcaeval_evidence import select_candidate_services

    _replace_metrics(
        recorded_case,
        disk_a_diskio=[1, 1, 1, 20, 20, 20, 20],
        disk_b_diskio=[1, 1, 1, 21, 21, 21, 21],
        disk_c_diskio=[1, 1, 1, 19, 19, 19, 19],
        disk_d_diskio=[1, 1, 1, 22, 22, 22, 22],
        cpu_target_cpu=[1, 1, 1, 9, 9, 9, 9],
        latency_target_latency=[1, 1, 1, 7, 7, 7, 7],
    )
    recorded_case.services = [
        "cpu_target",
        "disk_a",
        "disk_b",
        "disk_c",
        "disk_d",
        "latency_target",
    ]

    selection = select_candidate_services(recorded_case, max_candidates=3)

    assert "cpu_target" in selection.candidates
    assert "latency_target" in selection.candidates
    assert len(set(selection.candidates) & {"disk_a", "disk_b", "disk_c", "disk_d"}) <= 1


def test_three_coincident_services_are_not_common_mode_in_large_catalog(recorded_case):
    from app.evaluation.rcaeval_evidence import select_candidate_services

    _replace_metrics(
        recorded_case,
        disk_a_diskio=[1, 1, 1, 20, 20, 20, 20],
        disk_b_diskio=[1, 1, 1, 21, 21, 21, 21],
        disk_c_diskio=[1, 1, 1, 19, 19, 19, 19],
        stable_d_cpu=[1, 1, 1, 1, 1, 1, 1],
        stable_e_cpu=[1, 1, 1, 1, 1, 1, 1],
        stable_f_cpu=[1, 1, 1, 1, 1, 1, 1],
        stable_g_cpu=[1, 1, 1, 1, 1, 1, 1],
    )
    recorded_case.services = [
        "disk_a",
        "disk_b",
        "disk_c",
        "stable_d",
        "stable_e",
        "stable_f",
        "stable_g",
    ]

    selection = select_candidate_services(recorded_case, max_candidates=3)

    assert any("direct:disk" in reasons for reasons in selection.reasons.values())


def test_candidates_are_serialized_by_stable_service_name(recorded_case):
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    _replace_metrics(
        recorded_case,
        zulu_cpu=[1, 1, 1, 20, 20, 20, 20],
        alpha_mem=[1, 1, 1, 10, 10, 10, 10],
        middle_latency=[1, 1, 1, 8, 8, 8, 8],
    )
    recorded_case.services = ["zulu", "middle", "alpha"]

    payload = build_initial_evidence(recorded_case, max_bytes=32_768).payload
    names = [candidate["service"] for candidate in payload["candidates"]]

    assert names == sorted(names)


def test_initial_bundle_fits_configured_tool_boundary_deterministically(recorded_case):
    from langchain_core.messages import ToolMessage

    from app.core.data_safety import sanitize_tool_message
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    columns = {
        f"service_{index}_cpu": [1, 1, 1, 10 + index, 10 + index, 10 + index, 10 + index]
        for index in range(8)
    }
    _replace_metrics(recorded_case, **columns)
    recorded_case.services = [f"service_{index}" for index in range(8)]

    first = build_initial_evidence(recorded_case, max_bytes=16_384)
    second = build_initial_evidence(recorded_case, max_bytes=16_384)
    safe = sanitize_tool_message(
        ToolMessage(content=first.text, tool_call_id="offline-gate", name="initial_evidence")
    )

    assert first.text == second.text
    assert first.sha256 == second.sha256
    assert first.byte_length <= 16_384 - 512
    assert "tool_result_truncated" not in str(safe.content)


def test_candidate_budget_reduction_records_detailed_evidence_omissions(recorded_case, monkeypatch):
    from app.core.diagnostic_evidence import EvidenceBudgetError
    from app.evaluation import rcaeval_evidence

    columns = {
        f"service_{index}_cpu": [1, 1, 1, 10 + index, 10 + index, 10 + index, 10 + index]
        for index in range(8)
    }
    _replace_metrics(recorded_case, **columns)
    recorded_case.services = [f"service_{index}" for index in range(8)]
    original = rcaeval_evidence.serialize_evidence_bundle

    def force_candidate_reduction(bundle, *, max_bytes):
        if len(bundle.candidates) > 2:
            raise EvidenceBudgetError("test budget requires at most two candidates")
        return original(bundle, max_bytes=max_bytes)

    monkeypatch.setattr(
        rcaeval_evidence,
        "serialize_evidence_bundle",
        force_candidate_reduction,
    )

    payload = rcaeval_evidence.build_initial_evidence(recorded_case, max_bytes=16_384).payload

    assert len(payload["candidates"]) == 2
    assert any(path.startswith("candidate_budget.services[") for path in payload["omitted_paths"])
    assert any("candidate detail" in limitation for limitation in payload["limitations"])


def test_topology_exposes_caller_to_callee_without_reverse_inference(recorded_case):
    import pandas as pd

    from app.evaluation.rcaeval_evidence import build_initial_evidence

    _replace_metrics(
        recorded_case,
        caller_latency=[1, 1, 1, 20, 20, 20, 20],
        callee_cpu=[1, 1, 1, 30, 30, 30, 30],
    )
    recorded_case.services = ["callee", "caller"]
    recorded_case.has_traces = True
    recorded_case.t = pd.DataFrame(
        [
            {
                "traceID": "pre",
                "spanID": "p1",
                "parentSpanID": "",
                "serviceName": "caller",
                "duration": 5,
                "_epoch": 120,
            },
            {
                "traceID": "pre",
                "spanID": "c1",
                "parentSpanID": "p1",
                "serviceName": "callee",
                "duration": 5,
                "_epoch": 120,
            },
            {
                "traceID": "post",
                "spanID": "p2",
                "parentSpanID": "",
                "serviceName": "caller",
                "duration": 50,
                "_epoch": 240,
            },
            {
                "traceID": "post",
                "spanID": "c2",
                "parentSpanID": "p2",
                "serviceName": "callee",
                "duration": 40,
                "_epoch": 240,
            },
        ]
    )

    payload = build_initial_evidence(recorded_case, max_bytes=32_768).payload
    candidates = {item["service"]: item for item in payload["candidates"]}

    assert candidates["caller"]["outbound_dependencies"] == ["callee"]
    assert candidates["callee"]["inbound_callers"] == ["caller"]
    assert candidates["caller"]["inbound_callers"] == []
    assert candidates["callee"]["outbound_dependencies"] == []


def test_followup_topology_owns_each_edge_reference_once(recorded_case):
    import pandas as pd

    from app.core.diagnostic_evidence import EvidenceRequest, InitialEvidenceBundle
    from app.evaluation.rcaeval_evidence import (
        build_followup_evidence,
        build_initial_evidence,
    )

    _replace_metrics(
        recorded_case,
        caller_latency=[1, 1, 1, 20, 20, 20, 20],
        callee_cpu=[1, 1, 1, 30, 30, 30, 30],
    )
    recorded_case.services = ["callee", "caller"]
    recorded_case.has_traces = True
    recorded_case.t = pd.DataFrame(
        [
            {
                "traceID": "post",
                "spanID": "parent",
                "parentSpanID": "",
                "serviceName": "caller",
                "duration": 100,
                "_epoch": 240,
            },
            {
                "traceID": "post",
                "spanID": "child",
                "parentSpanID": "parent",
                "serviceName": "callee",
                "duration": 90,
                "_epoch": 240,
            },
        ]
    )
    initial = build_initial_evidence(recorded_case, max_bytes=32_768).payload
    initial_bundle = InitialEvidenceBundle.model_validate(initial)
    topology_ref = next(
        fact["evidence_ref"]
        for candidate in initial["candidates"]
        for fact in candidate["facts"]
        if fact["evidence_type"] == "topology_edge"
    )
    request = EvidenceRequest(
        services=("callee", "caller"),
        mechanisms=("cpu", "delay"),
        view="topology",
        justification_refs=(topology_ref,),
    )

    payload = build_followup_evidence(
        recorded_case,
        request,
        initial=initial_bundle,
        max_bytes=32_768,
    ).payload
    refs = [
        fact["evidence_ref"] for candidate in payload["candidates"] for fact in candidate["facts"]
    ]

    assert len(refs) == len(set(refs))


def test_missing_topology_is_explicitly_unavailable(recorded_case):
    import pandas as pd

    from app.evaluation.rcaeval_evidence import build_initial_evidence

    _replace_metrics(recorded_case, orders_cpu=[1, 1, 1, 20, 20, 20, 20])
    recorded_case.has_traces = False
    recorded_case.t = pd.DataFrame(columns=["_epoch", "serviceName"])

    payload = build_initial_evidence(recorded_case, max_bytes=32_768).payload

    assert any("Topology unavailable" in item for item in payload["limitations"])
    assert payload["candidates"][0]["inbound_callers"] == []
    assert payload["candidates"][0]["outbound_dependencies"] == []


def test_hint_expansion_is_bounded_to_catalog_evidence(recorded_case):
    from app.core.diagnostic_evidence import (
        EvidenceContractError,
        EvidenceRequest,
        InitialEvidenceBundle,
        validate_evidence_request,
    )
    from app.evaluation.rcaeval_evidence import (
        build_followup_evidence,
        build_initial_evidence,
    )

    columns = {
        f"service_{index}_cpu": [1, 1, 1, 10 + index, 10 + index, 10 + index, 10 + index]
        for index in range(6)
    }
    _replace_metrics(recorded_case, **columns)
    recorded_case.services = [f"service_{index}" for index in range(6)]
    initial = build_initial_evidence(recorded_case, max_bytes=65_536).payload
    initial_bundle = InitialEvidenceBundle.model_validate(initial)
    initial_names = {item["service"] for item in initial["candidates"]}
    hint = initial["candidate_hints"][0]
    selected = sorted(initial_names)[0]
    request = EvidenceRequest(
        services=(selected, hint["service"]),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=(hint["evidence_refs"][0],),
    )

    followup = build_followup_evidence(
        recorded_case,
        request,
        initial=initial_bundle,
        max_bytes=32_768,
    ).payload

    assert {item["service"] for item in followup["candidates"]} == {
        selected,
        hint["service"],
    }
    assert followup["request"] == request.model_dump(mode="json")
    assert all(set(item["mechanism_profiles"]) == {"cpu"} for item in followup["candidates"])
    parity_initial = initial_bundle.model_copy(
        update={"services": (*initial_bundle.services, "catalog-only")}
    )
    invalid_rows = (
        (
            request.model_copy(update={"services": ("outside-catalog",)}),
            "request_service_outside_catalog",
        ),
        (
            request.model_copy(update={"services": ("catalog-only",)}),
            "request_expansion_not_authorized",
        ),
        (
            request.model_copy(update={"services": (hint["service"], "catalog-only")}),
            "request_expansion_not_authorized",
        ),
        (
            EvidenceRequest(
                services=(selected,),
                mechanisms=("cpu",),
                view="metric_trends",
                justification_refs=(hint["evidence_refs"][0],),
            ),
            "request_justification_not_authorized",
        ),
        (
            request.model_copy(update={"justification_refs": ("not-a-catalog-ref",)}),
            "request_justification_not_authorized",
        ),
    )
    for bad_request, expected_code in invalid_rows:
        with pytest.raises(EvidenceContractError) as direct:
            validate_evidence_request(bad_request, parity_initial)
        with pytest.raises(EvidenceContractError) as built:
            build_followup_evidence(
                recorded_case,
                bad_request,
                initial=parity_initial,
                max_bytes=32_768,
            )
        assert direct.value.code == expected_code
        assert built.value.code == direct.value.code


def _development_case(case_id):
    from app.evaluation.rcaeval import CaseView

    root = Path(__file__).resolve().parents[1] / ".runtime" / "rcaeval" / "heldout-18-v1" / "tasks"
    return CaseView(root / case_id, case_id)


def test_case_032_bundle_preserves_downstream_direction_and_resources():
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    payload = build_initial_evidence(_development_case("case-032"), max_bytes=65_536).payload
    candidates = {item["service"]: item for item in payload["candidates"]}

    assert "productcatalogservice" in candidates
    assert "recommendationservice" in candidates
    assert "productcatalogservice" in candidates["recommendationservice"]["outbound_dependencies"]
    assert "recommendationservice" in candidates["productcatalogservice"]["inbound_callers"]
    assert (
        candidates["productcatalogservice"]["mechanism_profiles"]["cpu"]["state"] == "direct_signal"
    )
    assert candidates["recommendationservice"]["mechanism_profiles"]["delay"]["state"] in {
        "direct_signal",
        "possible_symptom",
    }


def test_case_041_train_service_is_initial_or_hinted():
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    payload = build_initial_evidence(_development_case("case-041"), max_bytes=65_536).payload
    visible = {item["service"] for item in payload["candidates"]} | {
        item["service"] for item in payload["candidate_hints"]
    }

    assert "ts-train-service" in visible


def test_case_054_cpu_is_direct_and_delay_is_symptom():
    from app.evaluation.rcaeval_evidence import build_initial_evidence

    payload = build_initial_evidence(_development_case("case-054"), max_bytes=65_536).payload
    candidates = {item["service"]: item for item in payload["candidates"]}
    auth = candidates["ts-auth-service"]

    assert auth["mechanism_profiles"]["cpu"]["state"] == "direct_signal"
    assert auth["mechanism_profiles"]["delay"]["state"] == "possible_symptom"


@pytest.mark.asyncio
async def test_runner_builds_initial_bundle_once_and_shares_exact_bytes(recorded_case, monkeypatch):
    from app.agent.aiops.two_stage import CoordinatorResult
    from app.core.diagnosis_result import DiagnosisResult
    from app.evaluation import rcaeval_evidence
    from app.evaluation.rcaeval import run_protocol_v3_case

    original = rcaeval_evidence.build_initial_evidence
    builds = []
    seen = []

    def counted(view, *, max_bytes):
        builds.append(view.case_id)
        return original(view, max_bytes=max_bytes)

    async def single(task, initial, **kwargs):
        seen.append(("single_stage", initial.text, initial.sha256))
        return CoordinatorResult(
            result=DiagnosisResult(outcome="insufficient_evidence"),
            call_count=1,
            initial_hash=initial.sha256,
            termination_reason="single_stage_complete",
        )

    async def agent(task, initial, **kwargs):
        seen.append(("agent", initial.text, initial.sha256))
        return CoordinatorResult(
            result=DiagnosisResult(outcome="insufficient_evidence"),
            call_count=1,
            initial_hash=initial.sha256,
            termination_reason="direct_diagnosis",
        )

    monkeypatch.setattr(rcaeval_evidence, "build_initial_evidence", counted)
    result = await run_protocol_v3_case(
        recorded_case,
        {"task": "diagnose"},
        model_factory_for=lambda _strategy: lambda **_kwargs: None,
        truth_loader=lambda _case_id: {"root_cause_service": "orders", "fault": "cpu"},
        single_runner=single,
        agent_runner=agent,
    )

    assert builds == ["case-001"]
    assert seen[0][1:] == seen[1][1:]
    assert result["parity"] is True
    assert all(item["protocol_version"] == 3 for item in result["runs"])
    assert all(item["valid_evidence_refs"] for item in result["runs"])
    assert all(
        all(ref.startswith("rca-") for ref in item["valid_evidence_refs"])
        for item in result["runs"]
    )


@pytest.mark.asyncio
async def test_runner_defaults_to_configured_tool_result_boundary(recorded_case, monkeypatch):
    from app.agent.aiops.two_stage import CoordinatorResult
    from app.config import config
    from app.core.diagnosis_result import DiagnosisResult
    from app.evaluation import rcaeval_evidence
    from app.evaluation.rcaeval import run_protocol_v3_case

    original = rcaeval_evidence.build_initial_evidence
    observed_limits = []

    def capture_limit(view, *, max_bytes):
        observed_limits.append(max_bytes)
        return original(view, max_bytes=max_bytes)

    async def runner(task, initial, **kwargs):
        return CoordinatorResult(
            result=DiagnosisResult(outcome="insufficient_evidence"),
            call_count=1,
            initial_hash=initial.sha256,
            termination_reason="complete",
        )

    monkeypatch.setattr(rcaeval_evidence, "build_initial_evidence", capture_limit)
    result = await run_protocol_v3_case(
        recorded_case,
        {"task": "diagnose"},
        model_factory_for=lambda _strategy: lambda **_kwargs: None,
        truth_loader=lambda _case_id: {"root_cause_service": "orders", "fault": "cpu"},
        single_runner=runner,
        agent_runner=runner,
    )

    assert observed_limits == [config.tool_result_max_bytes]
    assert result["initial_bytes"] <= config.tool_result_max_bytes - 512


@pytest.mark.asyncio
async def test_truth_is_loaded_only_after_both_predictions(recorded_case):
    from app.agent.aiops.two_stage import CoordinatorResult
    from app.core.diagnosis_result import DiagnosisResult
    from app.evaluation.rcaeval import run_protocol_v3_case

    events = []

    async def single(task, initial, **kwargs):
        events.append("single_stage")
        return CoordinatorResult(
            result=DiagnosisResult(outcome="insufficient_evidence"),
            call_count=1,
            initial_hash=initial.sha256,
            termination_reason="single_stage_complete",
        )

    async def agent(task, initial, **kwargs):
        events.append("agent")
        return CoordinatorResult(
            result=DiagnosisResult(outcome="insufficient_evidence"),
            call_count=1,
            initial_hash=initial.sha256,
            termination_reason="direct_diagnosis",
        )

    def truth(_case_id):
        events.append("truth")
        return {"root_cause_service": "orders", "fault": "cpu"}

    await run_protocol_v3_case(
        recorded_case,
        {"task": "diagnose"},
        model_factory_for=lambda _strategy: lambda **_kwargs: None,
        truth_loader=truth,
        single_runner=single,
        agent_runner=agent,
    )
    assert events == ["single_stage", "agent", "truth"]


def test_legacy_baseline_alias_is_explicit_not_silent():
    from app.evaluation.rcaeval import canonical_strategy

    assert canonical_strategy("baseline") == ("single_stage", "baseline")
    assert canonical_strategy("single_stage") == ("single_stage", None)


def test_candidate_context_keeps_relative_rank_and_inbound_callers(recorded_case):
    import pandas as pd

    _replace_metrics(
        recorded_case,
        **{
            "currency_latency-90": [1, 1, 1, 20, 20, 20, 20],
            "frontend_latency-90": [1, 1, 1, 5, 5, 5, 5],
        },
    )
    recorded_case.services = ["currency", "frontend"]
    recorded_case.t = pd.DataFrame(
        [
            {
                "traceID": "pre",
                "spanID": "parent",
                "parentSpanID": "",
                "serviceName": "frontend",
                "duration": 5,
                "_epoch": 120,
            },
            {
                "traceID": "pre",
                "spanID": "child",
                "parentSpanID": "parent",
                "serviceName": "currency",
                "duration": 10,
                "_epoch": 120,
            },
            {
                "traceID": "post",
                "spanID": "parent",
                "parentSpanID": "",
                "serviceName": "frontend",
                "duration": 50,
                "_epoch": 240,
            },
            {
                "traceID": "post",
                "spanID": "child",
                "parentSpanID": "parent",
                "serviceName": "currency",
                "duration": 100,
                "_epoch": 240,
            },
        ]
    )
    bundle = recorded_case.candidate_evidence(["currency", "frontend"])
    contexts = {row["service"]: row["relative_context"] for row in bundle["candidates"]}
    assert contexts["currency"]["anomaly_rank"] == 1
    assert contexts["currency"]["inbound_callers"] == ["frontend"]
    assert contexts["frontend"]["outbound_dependencies"] == ["currency"]


def test_logs_default_to_incident_window(recorded_case):
    assert recorded_case.logs(limit=1)["records"][0]["timestamp"] == 100
    assert "nearest" in recorded_case.logs()["sampling"]


def test_call_edges_join_within_trace_only(recorded_case):
    import pandas as pd

    recorded_case.t = pd.DataFrame(
        [
            {
                "traceID": "a",
                "spanID": "p",
                "parentSpanID": "",
                "serviceName": "caller-a",
                "duration": 5,
                "_epoch": 100,
            },
            {
                "traceID": "b",
                "spanID": "p",
                "parentSpanID": "",
                "serviceName": "caller-b",
                "duration": 6,
                "_epoch": 100,
            },
            {
                "traceID": "a",
                "spanID": "c",
                "parentSpanID": "p",
                "serviceName": "orders",
                "duration": 10,
                "_epoch": 100,
            },
            {
                "traceID": "b",
                "spanID": "c",
                "parentSpanID": "p",
                "serviceName": "orders",
                "duration": 90,
                "_epoch": 102,
            },
        ]
    )
    edges = recorded_case.call_edges()["edges"]
    assert len(edges) == 2
    assert sum(edge["spans"] for edge in edges) == 2
    assert next(e for e in edges if e["caller"] == "caller-a")["after_child_span_p90_us"] is None


@pytest.mark.asyncio
async def test_agent_reads_typed_report_without_extra_model_call(recorded_case, monkeypatch):
    from types import SimpleNamespace

    from app.core.llm_factory import llm_factory
    from app.evaluation.rcaeval import run
    from app.services import aiops_service

    class FakeGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"evidence": []})

    class FakeService:
        def _build_graph(self, saver):
            return FakeGraph()

        async def execute(self, task, session_id):
            yield {
                "diagnosis_result": {
                    "root_cause_service": "orders",
                    "fault_type": "unknown",
                    "outcome": "insufficient_evidence",
                    "summary": "Need mechanism evidence",
                }
            }

    def forbidden_model(**kwargs):
        raise AssertionError("Agent scoring must not call another model")

    monkeypatch.setattr(aiops_service, "AIOpsService", FakeService)
    monkeypatch.setattr(llm_factory, "create_chat_model", forbidden_model)
    result = await run(recorded_case, max_calls=2, timeout=5)
    assert result["errors"] == []
    assert result["prediction"]["service"] == "orders"
    assert result["prediction"]["fault"] == "unknown"
    assert result["usage"]["model_calls"] == 0


def test_rcaeval_adapter_contract():
    from app.evaluation import rcaeval

    root = Path(".runtime/rcaeval/pilot")
    if not (root / "cases.parquet").exists():
        pytest.skip("local RCAEval pilot not downloaded")
    adapter = rcaeval.Pilot(root)
    assert len(adapter.ids) == 3
    view = adapter.open("case-001")
    assert "re2ob_" not in str(view.public_task())
    assert "root_cause" not in str(view.public_task())
    assert view.metrics()["data_points"]
    assert view.logs()["records"]
    assert view.traces()["data_points"]
    assert len(view.logs(limit=2)["records"]) <= 2
    with pytest.raises(ValueError):
        view.logs(limit=1000)
    with pytest.raises(ValueError):
        view.metrics(service_name="../../.env")
    with pytest.raises(ValueError):
        adapter.open("../cases.parquet")
    with pytest.raises(ValueError):
        view.logs(start=view.end + 1)


def test_pilot_loads_anonymous_case_manifest(tmp_path):
    from app.evaluation.rcaeval import Pilot

    manifest = {
        "schema_version": 1,
        "dataset": "RCAEval RE2 stratified",
        "split": "heldout_model_blind",
        "cases": [
            {"case_id": "case-002", "case": "re2ss_orders_cpu_1"},
            {"case_id": "case-001", "case": "re2ob_checkoutservice_delay_2"},
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    pilot = Pilot(tmp_path)

    assert list(pilot.ids) == ["case-001", "case-002"]
    assert pilot.ids["case-001"] == "re2ob_checkoutservice_delay_2"
    assert pilot.dataset == "RCAEval RE2 stratified"
    assert pilot.split == "heldout_model_blind"


def test_pilot_rejects_unsafe_or_duplicate_manifest_entries(tmp_path):
    from app.evaluation.rcaeval import Pilot

    manifest = {
        "schema_version": 1,
        "cases": [
            {"case_id": "case-001", "case": "safe_case"},
            {"case_id": "case-001", "case": "../cases.parquet"},
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        Pilot(tmp_path)


def test_pilot_reads_ground_truth_from_separate_answers_file(tmp_path):
    import pandas as pd

    from app.evaluation.rcaeval import Pilot

    manifest = {
        "schema_version": 1,
        "cases": [{"case_id": "case-001", "case": "re2ob_checkoutservice_cpu_1"}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    answers = tmp_path.parent / "sealed-answers.parquet"
    pd.DataFrame(
        [
            {
                "case": "re2ob_checkoutservice_cpu_1",
                "root_cause_service": "checkoutservice",
                "fault": "cpu",
            }
        ]
    ).to_parquet(answers)

    pilot = Pilot(tmp_path, answers_file=answers)

    assert pilot.truth("case-001")["root_cause_service"] == "checkoutservice"
    assert pilot.answers_file == answers.resolve()


def test_pilot_scores_fully_anonymous_directory_by_case_id(tmp_path):
    import pandas as pd

    from app.evaluation.rcaeval import Pilot

    manifest = {
        "schema_version": 1,
        "cases": [{"case_id": "case-001", "case": "case-001"}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    answers = tmp_path.parent / "anonymous-answers.parquet"
    pd.DataFrame(
        [
            {
                "case_id": "case-001",
                "root_cause_service": "checkoutservice",
                "fault": "cpu",
            }
        ]
    ).to_parquet(answers)

    pilot = Pilot(tmp_path, answers_file=answers)

    assert pilot.truth("case-001")["fault"] == "cpu"


def test_scoring_requires_a_prediction_and_valid_service():
    from app.evaluation.rcaeval import score

    truth = {"root_cause_service": "orders", "fault": "cpu"}
    assert score({"service": "orders", "fault": "cpu"}, truth)["joint_correct"]
    assert not score({"service": "", "fault": "unknown"}, truth)["joint_correct"]


@pytest.mark.asyncio
async def test_runtime_initial_plan_bypasses_planning_dependencies(monkeypatch):
    import importlib

    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime

    planner_module = importlib.import_module("app.agent.aiops.planner")

    async def forbidden_lookup(*args, **kwargs):
        raise AssertionError("fixed plan must bypass knowledge lookup")

    monkeypatch.setattr(planner_module.retrieve_knowledge, "ainvoke", forbidden_lookup)
    monkeypatch.setattr(
        planner_module,
        "create_model",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("fixed plan must bypass the planning model")
        ),
    )
    with diagnosis_runtime(
        DiagnosisRuntime(context="evaluation", initial_plan=("collect candidate evidence",))
    ):
        result = await planner_module.planner({"input": "offline"})
    assert result == {"plan": ["collect candidate evidence"]}


@pytest.mark.asyncio
async def test_rcaeval_agent_uses_one_candidate_plan_and_hides_seeded_overview(
    recorded_case, monkeypatch
):
    from types import SimpleNamespace

    from app.agent.aiops.runtime import current_runtime
    from app.evaluation.rcaeval import run
    from app.services import aiops_service

    observed = {}

    class FakeGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"evidence": []})

    class FakeService:
        def _build_graph(self, saver):
            return FakeGraph()

        async def execute(self, task, session_id):
            runtime = current_runtime()
            observed["tools"] = [tool.name for tool in runtime.tools]
            observed["plan"] = runtime.initial_plan
            yield {
                "diagnosis_result": {
                    "root_cause_service": "orders",
                    "fault_type": "unknown",
                    "outcome": "insufficient_evidence",
                    "summary": "offline",
                }
            }

    monkeypatch.setattr(aiops_service, "AIOpsService", FakeService)
    result = await run(recorded_case, max_calls=2, timeout=5)
    assert result["errors"] == []
    assert observed["tools"] == ["query_candidate_metrics_evidence"]
    assert len(observed["plan"]) == 1
    assert "orders" in observed["plan"][0]


@pytest.mark.asyncio
async def test_fixed_overview_reaches_graph_before_planning(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.services import aiops_service

    seen = []

    async def plan(state):
        seen.extend(state["evidence"])
        return {"plan": []}

    async def execute(state):
        return {}

    async def respond(state):
        return {"response": "offline complete"}

    monkeypatch.setattr(aiops_service, "planner", plan)
    monkeypatch.setattr(aiops_service, "executor", execute)
    monkeypatch.setattr(aiops_service, "replanner", respond)
    seed = {"source": "query_diagnostic_metrics_overview", "evidence_ref": "fixed-evidence"}
    with diagnosis_runtime(
        DiagnosisRuntime(context="evaluation", tools=[], initial_evidence=(seed,))
    ):
        service = aiops_service.AIOpsService()
        service.graph = service._build_graph(MemorySaver())
        _ = [event async for event in service.execute("offline test", session_id="seed-test")]
    assert seen == [seed]
