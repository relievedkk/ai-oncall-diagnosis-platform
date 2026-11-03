import pytest

from app.core.single_pass import EvidenceSet, ObservationWindow, fact_for
from app.evaluation.single_pass_hypotheses import rank_hypotheses, supported_service

WINDOW = ObservationWindow(start=0, incident=10, end=30)


def evidence(family, *, specific=False, workload_changed=False, competing=False, known=True):
    facts = []
    for name in ("cpu", "mem", "disk", "socket", "loss", "delay", "workload"):
        changed = name in {family, "delay"} or (name == "workload" and workload_changed)
        if competing and name == "mem":
            changed = True
        facts.append(
            fact_for(
                "case",
                WINDOW,
                "a",
                "metrics",
                name,
                {
                    "coverage": "complete",
                    "changed": changed,
                    "direction": "up",
                    "unit": "percent" if known else "unknown",
                    "metric_kind": "gauge",
                    "metric": "a_" + name,
                    "before_samples": 10,
                    "after_samples": 20,
                    "onset_lower": 9,
                    "onset_upper": 10,
                    "persistence": 1.0,
                },
            )
        )
    if specific:
        event = {
            "cpu": "cpu_throttling",
            "mem": "memory_allocation",
            "disk": "disk_error",
            "socket": "socket_limit",
            "loss": "packet_loss",
            "delay": "timeout",
        }[family]
        facts.append(
            fact_for(
                "case",
                WINDOW,
                "a",
                "logs",
                event,
                {
                    "specific": True,
                    "new_event": True,
                    "after_buckets": 3,
                },
            )
        )
    return EvidenceSet(case_id="case", window=WINDOW, services=("a",), facts=tuple(facts))


@pytest.mark.parametrize(
    "family,specific",
    [
        ("cpu", False),
        ("mem", True),
        ("disk", True),
        ("socket", True),
        ("loss", False),
        ("delay", False),
    ],
)
def test_six_mechanisms_have_explicit_support_and_discriminating_contrast(family, specific):
    e = evidence(family, specific=specific)
    h = next(h for h in rank_hypotheses(e) if h.fault == family)
    assert h.eligible
    assert h.supporting_refs and h.contrast_refs
    assert supported_service(e, "a")


@pytest.mark.parametrize("family", ["cpu", "mem", "disk", "socket", "delay"])
def test_load_or_competing_resource_blocks_unresolved_mechanism(family):
    e = evidence(family, workload_changed=True, competing=family != "mem")
    h = next(h for h in rank_hypotheses(e, limit=6) if h.fault == family)
    assert not h.eligible
    assert h.gaps


def test_keyword_timeout_never_proves_loss_or_socket():
    e = evidence("delay")
    log = fact_for(
        "case",
        WINDOW,
        "a",
        "logs",
        "timeout",
        {"specific": False, "new_event": True, "after_buckets": 100},
    )
    e = e.model_copy(update={"facts": e.facts + (log,)})
    assert not next(h for h in rank_hypotheses(e, limit=6) if h.fault == "loss").eligible
    assert not next(h for h in rank_hypotheses(e, limit=6) if h.fault == "socket").eligible


def test_unknown_cpu_semantics_and_io_decrease_are_not_confirmation():
    e = evidence("cpu", known=False)
    assert not next(h for h in rank_hypotheses(e) if h.fault == "cpu").eligible
    e = evidence("disk")
    e = e.model_copy(
        update={
            "facts": tuple(
                f.model_copy(update={"data": {**f.data, "direction": "down"}})
                if f.family == "disk"
                else f
                for f in e.facts
            )
        }
    )
    assert not next(h for h in rank_hypotheses(e) if h.fault == "disk").eligible


def test_specific_service_can_survive_unknown_mechanism_and_ranking_is_bounded():
    e = evidence("mem", specific=True, competing=True, workload_changed=True)
    assert supported_service(e, "a")
    assert not supported_service(e, "invented")
    assert len(rank_hypotheses(e)) <= 5
    assert rank_hypotheses(e) == rank_hypotheses(e)


def test_two_equally_specific_mechanisms_cannot_both_be_confirmed():
    e = evidence("mem", specific=True)
    competing = fact_for(
        "case",
        WINDOW,
        "a",
        "logs",
        "disk_error",
        {"specific": True, "new_event": True, "after_buckets": 3},
    )
    e = e.model_copy(update={"facts": e.facts + (competing,)})
    assert not any(h.eligible for h in rank_hypotheses(e, limit=6))
    h = next(h for h in rank_hypotheses(e, limit=6) if h.fault == "mem")
    assert competing.ref in h.opposing_refs


def test_incident_alignment_breaks_retrieval_ties_without_granting_permission():
    e = evidence("cpu", known=False)
    early = tuple(f.model_copy(update={"service": "z", "ref": "z-" + f.ref}) for f in e.facts)
    late = tuple(
        f.model_copy(update={"data": {**f.data, "onset_lower": 25, "onset_upper": 28}})
        if f.data.get("changed")
        else f
        for f in e.facts
    )
    e = e.model_copy(update={"services": ("a", "z"), "facts": early + late})
    ranked = rank_hypotheses(e)
    assert ranked[0].service == "z"
    assert not any(h.eligible for h in ranked)


def test_missing_baseline_observation_remains_a_candidate_with_explicit_gap():
    e = evidence("cpu", known=False)
    fs = tuple(
        f.model_copy(
            update={
                "data": {
                    **f.data,
                    "coverage": "baseline_missing",
                    "before_median": None,
                    "after_median": 100.0,
                    "after_samples": 20,
                    "before_samples": 0,
                    "changed": False,
                }
            }
        )
        if f.family == "disk"
        else f
        for f in e.facts
    )
    e = e.model_copy(update={"facts": fs})
    h = next(h for h in rank_hypotheses(e) if h.fault == "disk")
    disk = next(f for f in fs if f.family == "disk")
    assert disk.ref in h.supporting_refs
    assert "missing_baseline_for_mechanism" in h.gaps and not h.eligible


def test_persistent_subthreshold_socket_is_retrieved_without_connection_permission():
    e = evidence("cpu", known=False)
    e = e.model_copy(
        update={
            "facts": tuple(
                f.model_copy(
                    update={
                        "data": {
                            **f.data,
                            "before_median": 20.0,
                            "after_median": 28.0,
                            "onset_upper": 10,
                            "persistence": 0.5,
                        }
                    }
                )
                if f.family == "socket"
                else f
                for f in e.facts
            )
        }
    )
    h = next(h for h in rank_hypotheses(e) if h.fault == "socket")
    assert h.supporting_refs and "retrieval_only_observation" in h.gaps
    assert not h.eligible


def test_network_ambiguity_keeps_both_explanations_for_top_two_endpoints():
    from app.core.single_pass import EvidenceSet

    seed = evidence("delay", known=False)
    facts = []
    services = tuple("s" + str(i) for i in range(5))
    for i, service in enumerate(services):
        for f in seed.facts:
            if f.family == "loss":
                continue
            data = {**f.data, "onset_upper": 10 + i * 20}
            facts.append(
                f.model_copy(update={"service": service, "ref": service + f.ref, "data": data})
            )
    e = EvidenceSet(case_id=seed.case_id, window=seed.window, services=services, facts=tuple(facts))
    hs = rank_hypotheses(e)
    assert len(hs) == 5
    assert {("s0", "delay"), ("s0", "loss"), ("s1", "delay"), ("s1", "loss")} <= {
        (h.service, h.fault) for h in hs
    }
    assert not any(h.eligible for h in hs)
    assert all("missing_mechanism_specific_observation" in h.gaps for h in hs if h.fault == "loss")


def test_shared_resource_changes_do_not_displace_distinct_memory_hypothesis():
    from app.core.single_pass import EvidenceSet

    seed = evidence("disk", known=False)
    facts = []
    services = tuple("s" + str(i) for i in range(8))
    for service in services:
        for f in seed.facts:
            data = {**f.data}
            if service == "s7" and f.family == "mem":
                data["changed"] = True
            facts.append(
                f.model_copy(update={"service": service, "ref": service + f.ref, "data": data})
            )
    e = EvidenceSet(case_id=seed.case_id, window=seed.window, services=services, facts=tuple(facts))
    hs = rank_hypotheses(e)
    assert any(h.service == "s7" and h.fault == "mem" for h in hs)
    assert not any(h.eligible for h in hs)


def test_partial_delay_observation_never_becomes_a_qualified_latency_change():
    e = evidence("delay")
    fs = tuple(
        f.model_copy(
            update={
                "data": {
                    **f.data,
                    "coverage": "baseline_missing",
                    "before_median": None,
                    "after_median": 10.0,
                    "before_samples": 0,
                    "after_samples": 20,
                    "changed": False,
                }
            }
        )
        if f.family == "delay"
        else f
        for f in e.facts
    )
    error = fact_for(
        "case",
        WINDOW,
        "a",
        "metrics",
        "error",
        {"changed": True, "coverage": "complete", "before_median": 0.0, "after_median": 2.0},
    )
    e = e.model_copy(update={"facts": fs + (error,)})
    h = next(h for h in rank_hypotheses(e, limit=6) if h.fault == "delay")
    assert h.supporting_refs and not h.eligible
