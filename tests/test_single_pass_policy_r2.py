from app.core.single_pass import EvidenceSet, fact_for
from app.evaluation.single_pass_hypotheses import (
    collect_evidence,
    rank_hypotheses,
    supported_service,
)
from app.evaluation.single_pass_packet import build_packet
from tests.single_pass_helpers import view_of
from tests.test_single_pass_hypotheses import evidence


def change(e, family, **data):
    return e.model_copy(
        update={
            "facts": tuple(
                f.model_copy(update={"data": {**f.data, **data}}) if f.family == family else f
                for f in e.facts
            )
        }
    )


def hypothesis(e, fault):
    return next(h for h in rank_hypotheses(e, limit=6) if h.fault == fault)


def test_localized_service_survives_unknown_cpu_measurement_semantics():
    e = evidence("cpu", known=False)
    assert not hypothesis(e, "cpu").eligible
    assert supported_service(e, "a")
    assert hypothesis(e, "cpu").candidate_supported


def test_sustained_documented_memory_is_not_misread_as_absence_of_memory_evidence():
    e = change(
        evidence("mem"), "mem", post_shape="sustained", unit="bytes", quantity="memory_usage"
    )
    h = hypothesis(e, "mem")
    assert h.candidate_supported and h.eligible
    assert "cache_or_allocation_growth" in h.alternatives


def test_memory_competition_keeps_tentative_candidate_but_blocks_confirmation():
    e = change(
        evidence("mem"), "mem", post_shape="sustained", unit="bytes", quantity="memory_usage"
    )
    e = change(e, "cpu", changed=True)
    h = hypothesis(e, "mem")
    assert h.candidate_supported and not h.eligible
    assert supported_service(e, "a")


def test_delay_complete_own_paths_do_not_require_a_missing_loss_series():
    e = evidence("delay")
    own = fact_for(
        e.case_id,
        e.window,
        "a",
        "traces",
        "operation",
        {
            "before_spans": 10,
            "after_spans": 10,
            "before_self_spans": 10,
            "after_self_spans": 10,
            "before_p90_us": 10,
            "after_p90_us": 40,
            "before_self_p90_us": 10,
            "after_self_p90_us": 40,
        },
    )
    edge = fact_for(
        e.case_id,
        e.window,
        "a",
        "traces",
        "edge",
        {
            "callee": "b",
            "before_spans": 10,
            "after_spans": 10,
            "before_p90_us": 10,
            "after_p90_us": 10,
        },
    )
    e = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.family != "loss") + (own, edge)}
    )
    stable_edge = edge.model_copy(
        update={"ref": "another-stable-edge", "data": {**edge.data, "callee": "c"}}
    )
    e = e.model_copy(update={"facts": e.facts + (stable_edge,)})
    h = hypothesis(e, "delay")
    assert h.eligible and {edge.ref, stable_edge.ref} <= set(h.contrast_refs)
    slow_edge = edge.model_copy(update={"data": {**edge.data, "after_p90_us": 100}})
    e = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.ref != edge.ref) + (slow_edge,)}
    )
    h = hypothesis(e, "delay")
    assert not h.eligible and slow_edge.ref in h.contrast_refs
    assert stable_edge.ref not in h.contrast_refs
    incomplete = edge.model_copy(
        update={
            "ref": "incomplete-edge",
            "data": {
                **edge.data,
                "before_spans": 2,
            },
        }
    )
    zero_baseline = stable_edge.model_copy(
        update={"data": {**stable_edge.data, "before_p90_us": 0}}
    )
    e = e.model_copy(
        update={
            "facts": tuple(f for f in e.facts if f.family != "edge") + (incomplete, zero_baseline)
        }
    )
    h = hypothesis(e, "delay")
    assert not h.eligible and {incomplete.ref, zero_baseline.ref} <= set(h.contrast_refs)


def test_socket_capacity_with_independent_failure_observation_can_confirm():
    e = change(
        evidence("socket"),
        "socket",
        unit="count",
        metric_kind="gauge",
        after_median=99,
        resource_limit=100,
    )
    event = fact_for(
        e.case_id,
        e.window,
        "a",
        "logs",
        "connection_error",
        {"specific": False, "new_event": True, "before_count": 0, "after_buckets": 3},
    )
    e = e.model_copy(update={"facts": e.facts + (event,)})
    assert hypothesis(e, "socket").eligible
    e = change(e, "socket", resource_limit=None)
    assert hypothesis(e, "socket").candidate_supported and not hypothesis(e, "socket").eligible


def test_boolean_capacity_cannot_grant_socket_confirmation():
    e = change(
        evidence("socket"),
        "socket",
        unit="count",
        metric_kind="gauge",
        after_median=99,
        resource_limit=True,
    )
    event = fact_for(
        e.case_id,
        e.window,
        "a",
        "logs",
        "connection_error",
        {"specific": False, "new_event": True, "after_buckets": 3},
    )
    e = e.model_copy(update={"facts": e.facts + (event,)})
    assert not hypothesis(e, "socket").eligible


def test_keyword_timeout_still_cannot_confirm_network_loss():
    e = evidence("delay")
    event = fact_for(
        e.case_id,
        e.window,
        "a",
        "logs",
        "timeout",
        {"specific": False, "new_event": True, "after_buckets": 100},
    )
    e = e.model_copy(update={"facts": e.facts + (event,)})
    assert not hypothesis(e, "loss").eligible


def test_uniform_rpc_wait_at_shared_endpoint_needs_comparable_control_paths():
    e = evidence("delay")
    metrics = [f for f in e.facts if f.family not in {"disk", "socket", "loss", "delay"}]
    metrics += [
        fact_for(
            e.case_id,
            e.window,
            "a",
            "metrics",
            "delay",
            {
                "coverage": "complete",
                "changed": True,
                "direction": "up",
                "metric": f"a_latency-{p}",
                "before_median": p,
                "after_median": p * 5,
                "onset_upper": 11,
            },
        )
        for p in (50, 90)
    ]
    metrics = [
        f.model_copy(update={"data": {**f.data, "before_median": 20, "after_median": 20}})
        if f.family == "workload"
        else f
        for f in metrics
    ]

    def rpc(caller, callee, wait):
        return fact_for(
            e.case_id,
            e.window,
            caller,
            "traces",
            "rpc_wait",
            {
                "callee": callee,
                "before_pairs": 20,
                "after_pairs": 20,
                "before_buckets": [-4, -3, -2, -1],
                "after_buckets": [0, 1, 2, 3],
                "before_wait_us": [1000, 1200, 1500],
                "after_wait_us": wait,
                "before_handler_p90_us": 500,
                "after_handler_p90_us": 510,
            },
        )

    first = rpc("b", "a", [41000, 41200, 41500])
    second = rpc("c", "a", [42000, 42200, 42500])
    controls = (rpc("b", "other", [990, 1210, 1490]), rpc("c", "other", [1010, 1190, 1520]))
    e = e.model_copy(
        update={
            "facts": tuple(metrics) + (first, second) + controls,
            "aligned_latency_quantiles": ("a",),
        }
    )
    h = hypothesis(e, "delay")
    assert h.candidate_supported and not h.eligible
    assert "rpc_wait_cause_unresolved" in h.gaps
    assert "packet_retransmission_or_path_delay" in h.alternatives
    assert {f.ref for f in (first, second) + controls} <= set(h.contrast_refs)
    without_control = e.model_copy(update={"facts": e.facts[:-1]})
    assert "rpc_wait_cause_unresolved" not in hypothesis(without_control, "delay").gaps
    disjoint = second.model_copy(update={"data": {**second.data, "after_buckets": [6, 7, 8, 9]}})
    nonoverlap = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.ref != second.ref) + (disjoint,)}
    )
    assert "rpc_wait_cause_unresolved" not in hypothesis(nonoverlap, "delay").gaps
    tail_only = rpc("c", "a", [1100, 1300, 41500])
    e = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.ref != second.ref) + (tail_only,)}
    )
    assert "rpc_wait_cause_unresolved" not in hypothesis(e, "delay").gaps


def test_delay_ranking_keeps_later_downstream_observation_as_contrast():
    e = evidence("delay")
    metrics = [f for f in e.facts if f.family != "delay"]
    for p in (50, 90):
        metrics.append(
            fact_for(
                e.case_id,
                e.window,
                "a",
                "metrics",
                "delay",
                {
                    "metric": f"a_latency-{p}",
                    "coverage": "complete",
                    "direction": "up",
                    "changed": True,
                    "before_median": p,
                    "after_median": p * 4,
                    "onset_upper": 11,
                },
            )
        )
    late = fact_for(
        e.case_id,
        e.window,
        "b",
        "metrics",
        "delay",
        {
            "metric": "b_latency-90",
            "coverage": "complete",
            "changed": True,
            "direction": "up",
            "onset_lower": 27,
            "onset_upper": 28,
        },
    )
    edges = tuple(
        fact_for(
            e.case_id,
            e.window,
            caller,
            "traces",
            "edge",
            {
                "callee": callee,
                "before_spans": 10,
                "after_spans": 10,
                "before_p90_us": 10,
                "after_p90_us": 100,
            },
        )
        for caller, callee in (("c", "a"), ("d", "a"), ("a", "b"))
    )
    e = e.model_copy(
        update={"facts": tuple(metrics) + edges + (late,), "aligned_latency_quantiles": ("a",)}
    )
    h = hypothesis(e, "delay")
    assert late.ref in h.contrast_refs and not h.eligible
    overlap = late.model_copy(update={"data": {**late.data, "onset_lower": 10}})
    e = e.model_copy(update={"facts": e.facts[:-1] + (overlap,)})
    assert late.ref not in hypothesis(e, "delay").contrast_refs


def test_short_failure_burst_remains_a_tentative_network_signal():
    e = change(evidence("delay"), "delay", scales={"30": 5, "300": 10})
    failure = fact_for(
        e.case_id,
        e.window,
        "a",
        "metrics",
        "error",
        {
            "coverage": "complete",
            "changed": False,
            "before_median": 0,
            "after_median": 0,
            "onset_upper": 12,
            "scales": {"30": 0.2, "300": 0},
        },
    )
    e = e.model_copy(update={"facts": e.facts + (failure,)})
    h = hypothesis(e, "loss")
    assert h.candidate_supported and not h.eligible
    assert failure.ref in h.supporting_refs
    assert next(f.ref for f in e.facts if f.family == "loss") in h.opposing_refs
    assert "request_errors_are_indirect" in h.gaps
    hypotheses = rank_hypotheses(e)
    full = build_packet(e, hypotheses)
    compact = build_packet(e, hypotheses, max_bytes=full.transport_bytes - 1)
    alias = next(alias for alias, ref in compact.ref_aliases if ref == failure.ref)
    fact = compact.payload["facts"][alias]
    assert (
        dict(zip(compact.payload["fact_columns"][fact["columns"]], fact["values"], strict=True))[
            "scales"
        ]
        == failure.data["scales"]
    )
    e = change(e, "error", sampling_gap=True)
    assert not hypothesis(e, "loss").candidate_supported


def test_sustained_resource_surge_ranks_above_small_concurrent_shift():
    e = change(evidence("disk", competing=True), "disk", before_median=2, after_median=512)
    e = change(e, "mem", before_median=100, after_median=170)
    assert rank_hypotheses(e)[0].fault == "disk"
    assert not hypothesis(e, "disk").eligible
    e = change(e, "disk", direction="down", before_median=512, after_median=2)
    assert rank_hypotheses(e)[0].fault != "disk"


def test_persistent_memory_growth_can_rank_above_a_concurrent_cpu_spike():
    e = change(
        evidence("mem"),
        "mem",
        before_median=10,
        after_median=70,
        quantity="memory_usage",
        unit="bytes",
        post_shape="sustained",
    )
    e = change(e, "cpu", changed=True, before_median=0.1, after_median=40)
    assert rank_hypotheses(e)[0].fault == "mem"
    assert not hypothesis(e, "mem").eligible and not hypothesis(e, "cpu").eligible


def test_socket_growth_can_rank_above_concurrent_cpu_without_proving_exhaustion():
    e = change(evidence("socket"), "socket", before_median=10, after_median=22)
    e = change(e, "cpu", changed=True, before_median=0.1, after_median=40)
    assert rank_hypotheses(e)[0].fault == "socket"
    assert not hypothesis(e, "socket").eligible


def test_tail_widening_requires_both_quantiles_and_is_only_tentative_loss():
    e = change(evidence("delay"), "delay", metric="a_latency-90", before_median=4, after_median=80)
    high = next(f for f in e.facts if f.family == "delay")
    median = fact_for(
        e.case_id,
        e.window,
        "a",
        "metrics",
        "delay",
        {
            **high.data,
            "metric": "a_latency-50",
            "before_median": 2,
            "after_median": 2.5,
            "changed": False,
        },
    )
    e = e.model_copy(update={"facts": e.facts + (median,), "aligned_latency_quantiles": ("a",)})
    h = hypothesis(e, "loss")
    assert h.candidate_supported and not h.eligible
    assert {high.ref, median.ref} <= set(h.supporting_refs)
    assert next(f.ref for f in e.facts if f.family == "loss") in h.opposing_refs
    assert not hypothesis(change(e, "delay", sampling_gap=True), "loss").candidate_supported
    inverted = median.model_copy(update={"data": {**median.data, "before_median": 5}})
    invalid = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.ref != median.ref) + (inverted,)}
    )
    assert not hypothesis(invalid, "loss").candidate_supported
    different_unit = inverted.model_copy(update={"data": {**inverted.data, "unit": "ms"}})
    other_units = e.model_copy(
        update={"facts": tuple(f for f in e.facts if f.ref != median.ref) + (different_unit,)}
    )
    assert hypothesis(other_units, "loss").candidate_supported
    shared = tuple(
        fact_for(
            e.case_id,
            e.window,
            "gateway",
            "traces",
            "edge",
            {
                "callee": callee,
                "before_spans": 10,
                "after_spans": 10,
                "before_p90_us": 10,
                "after_p90_us": 10,
                "before_client_spans": 10,
                "after_client_spans": 10,
                "before_client_p90_us": 12,
                "after_client_p90_us": 120,
            },
        )
        for callee in ("a", "b", "c")
    )
    probe = shared[0].model_copy(
        update={
            "service": "probe",
            "ref": "undersampled-probe",
            "data": {
                **shared[0].data,
                "before_spans": 1,
                "after_spans": 1,
                "before_client_spans": 1,
                "after_client_spans": 1,
            },
        }
    )
    blocked = e.model_copy(update={"facts": e.facts + shared + (probe,)})
    assert not hypothesis(blocked, "loss").candidate_supported
    broad = median.model_copy(update={"data": {**median.data, "after_median": 38, "changed": True}})
    e = e.model_copy(update={"facts": tuple(f for f in e.facts if f.ref != median.ref) + (broad,)})
    assert rank_hypotheses(e)[0].fault == "delay"
    assert not hypothesis(e, "loss").candidate_supported


def test_tail_widening_rejects_individually_complete_but_unpaired_samples():
    view = view_of(
        {
            "a_latency-50": [2] * 6 + [None] * 4,
            "a_latency-90": [4] * 3 + [None] * 3 + [80] * 4,
            "a_packet_loss": [0] * 10,
            "a_workload": [20] * 10,
        },
        times=[0, 40, 80, 100, 120, 140, 160, 200, 240, 280],
        incident=100,
    )
    e = collect_evidence(view)
    quantiles = [f for f in e.facts if f.family == "delay"]
    assert all(f.data["coverage"] == "complete" and not f.data["sampling_gap"] for f in quantiles)
    assert any(f.data["changed"] for f in quantiles)
    assert not hypothesis(e, "loss").candidate_supported
    view.m["a_latency-50"] = [2] * 3 + [None] * 3 + [2] * 4
    assert hypothesis(collect_evidence(view), "loss").candidate_supported


def test_peer_comparison_keeps_missing_baseline_resource_surge_tentative():
    e = change(
        evidence("disk"),
        "disk",
        before_median=None,
        after_median=2048,
        coverage="baseline_missing",
        changed=False,
    )
    disk = next(f for f in e.facts if f.family == "disk")
    peer = disk.model_copy(
        update={
            "ref": "peer-disk",
            "service": "b",
            "data": {
                **disk.data,
                "metric": "b_disk",
                "before_median": 1,
                "after_median": 2,
                "coverage": "complete",
                "changed": True,
            },
        }
    )
    e = e.model_copy(update={"services": ("a", "b"), "facts": e.facts + (peer,)})
    h = rank_hypotheses(e)[0]
    assert (h.service, h.fault) == ("a", "disk")
    assert h.candidate_supported and not h.eligible


def test_slow_incoming_edge_localizes_callee_with_stable_downstream():
    e = evidence("delay")
    facts = tuple(f for f in e.facts if f.family in {"delay", "workload"})
    facts += tuple(f.model_copy(update={"service": "b", "ref": "b" + f.ref}) for f in facts)
    incoming = fact_for(
        e.case_id,
        e.window,
        "b",
        "traces",
        "edge",
        {
            "callee": "a",
            "before_spans": 10,
            "after_spans": 10,
            "before_p90_us": 10,
            "after_p90_us": 40,
        },
    )
    outgoing = fact_for(
        e.case_id,
        e.window,
        "a",
        "traces",
        "edge",
        {
            "callee": "c",
            "before_spans": 10,
            "after_spans": 10,
            "before_p90_us": 10,
            "after_p90_us": 10,
        },
    )
    e = e.model_copy(update={"services": ("a", "b", "c"), "facts": facts + (incoming, outgoing)})
    h = rank_hypotheses(e)[0]
    assert (h.service, h.fault) == ("a", "delay")
    assert incoming.ref in h.contrast_refs
    packet = build_packet(e, (h,))
    alias = next(alias for alias, ref in packet.ref_aliases if ref == incoming.ref)
    payload = packet.payload
    fact = payload["facts"][alias]
    data = dict(zip(payload["fact_columns"][fact["columns"]], fact["values"], strict=True))
    assert fact["service"] is None and data["caller"] == "b"
    assert supported_service(e, "a") and not h.eligible
    assert not supported_service(e, "b")
    assert not supported_service(e.model_copy(update={"facts": facts + (incoming,)}), "a")
    slower_outgoing = outgoing.model_copy(update={"data": {**outgoing.data, "after_p90_us": 100}})
    e = e.model_copy(update={"facts": facts + (incoming, slower_outgoing)})
    assert not supported_service(e, "a")

    # A fast handler can still be reached through a slow RPC.
    paired_slow_outgoing = outgoing.model_copy(
        update={
            "data": {
                **outgoing.data,
                "before_client_spans": 10,
                "after_client_spans": 10,
                "before_client_p90_us": 12,
                "after_client_p90_us": 100,
            }
        }
    )
    e = e.model_copy(update={"facts": facts + (incoming, paired_slow_outgoing)})
    assert not supported_service(e, "a")

    slow_client = incoming.model_copy(
        update={
            "data": {
                **paired_slow_outgoing.data,
                "callee": "a",
            }
        }
    )
    e = e.model_copy(update={"facts": facts + (slow_client, outgoing)})
    assert not supported_service(e, "a")
    second_client = slow_client.model_copy(update={"service": "c", "ref": "second-client"})
    e = e.model_copy(update={"facts": facts + (slow_client, second_client)})
    assert supported_service(e, "a")
    assert not any(h.eligible for h in rank_hypotheses(e))


def test_plain_disk_errno_needs_independent_disk_shift_and_response():
    e = evidence("disk", known=False)
    f = fact_for(
        e.case_id,
        e.window,
        "a",
        "logs",
        "disk_error",
        {"literal_errno": "EIO", "specific": False, "new_event": True, "after_buckets": 3},
    )
    e = e.model_copy(update={"facts": e.facts + (f,)})
    assert hypothesis(e, "disk").eligible
    e = change(e, "disk", changed=False)
    assert not hypothesis(e, "disk").eligible


def test_unchanged_preexisting_gc_does_not_veto_a_cpu_diagnosis():
    e = evidence("cpu")
    f = fact_for(
        e.case_id,
        e.window,
        "a",
        "logs",
        "gc",
        {"specific": False, "new_event": False, "before_buckets": 4, "after_buckets": 4},
    )
    e = e.model_copy(update={"facts": e.facts + (f,)})
    assert hypothesis(e, "cpu").eligible


def test_retrieval_reserves_slots_for_distinct_services_before_filling_alternatives():
    seed = evidence("cpu", known=False)
    facts = []
    for svc in ("a", "b", "c"):
        for f in seed.facts:
            d = {**f.data}
            if svc == "a" and f.family in {"mem", "socket", "disk"}:
                d["changed"] = True
            facts.append(f.model_copy(update={"service": svc, "ref": svc + f.ref, "data": d}))
    e = EvidenceSet(
        case_id=seed.case_id, window=seed.window, services=("a", "b", "c"), facts=tuple(facts)
    )
    hs = rank_hypotheses(e)
    assert {h.service for h in hs} == {"a", "b", "c"}


def test_empty_service_slots_make_room_for_supported_alternatives():
    e = change(evidence("disk", competing=True), "socket", changed=True)
    quiet_services = tuple(
        f.model_copy(
            update={
                "service": service,
                "ref": service + f.ref,
                "data": {**f.data, "changed": False},
            }
        )
        for service in ("b", "c")
        for f in e.facts
    )
    e = e.model_copy(update={"services": ("a", "b", "c"), "facts": e.facts + quiet_services})
    assert {(h.service, h.fault) for h in rank_hypotheses(e)} == {
        ("a", "disk"),
        ("a", "mem"),
        ("a", "socket"),
        ("a", "delay"),
    }


def test_service_first_retrieval_fills_types_within_three_selected_services():
    seed = evidence("cpu", known=False)
    services = tuple("abcdef")
    facts = tuple(
        f.model_copy(
            update={
                "service": s,
                "ref": s + f.ref,
                "data": {**f.data, "onset_upper": 29} if f.family == "delay" else f.data,
            }
        )
        for s in services
        for f in seed.facts
    )
    e = EvidenceSet(case_id=seed.case_id, window=seed.window, services=services, facts=facts)
    hs = rank_hypotheses(e)
    assert len(hs) == 5 and len({h.service for h in hs}) == 3
