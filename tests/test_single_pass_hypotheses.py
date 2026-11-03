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
