import json

import pytest

from app.agent.aiops.single_pass import SinglePassDecision, render_report, validate_decision
from app.evaluation.single_pass_hypotheses import rank_hypotheses
from app.evaluation.single_pass_packet import build_packet
from tests.test_single_pass_hypotheses import evidence


def decision(packet, h=None, **updates):
    payload = json.loads(packet.text)
    h = h or next(h for h in payload["hypotheses"] if h["eligible"])
    values = {
        "packet_id": payload["packet_id"],
        "service_id": h["service"],
        "fault": h["fault"],
        "supporting_refs": h["support"],
        "opposing_refs": h["opposition"],
        "contrast_refs": h["contrast"],
        "reason": "observations and contrasts",
        "gaps": [],
        "excluded_hypotheses": [],
    }
    return SinglePassDecision(**{**values, **updates})


def test_packet_keeps_all_material_refs_and_schema_transport_within_budget():
    e = evidence("cpu")
    hypotheses = rank_hypotheses(e)
    packet = build_packet(e, hypotheses)
    data = json.loads(packet.text)
    assert packet.transport_bytes <= 16384
    assert len(data["hypotheses"]) <= 5
    for h in data["hypotheses"]:
        assert set(h["support"] + h["opposition"] + h["contrast"]) <= set(data["facts"])
    result = validate_decision(packet, decision(packet))
    assert result["service"] == "a" and result["fault"] == "cpu"
    assert "a" in render_report(result)


def test_required_ref_budget_overflow_fails_instead_of_dropping_opposition():
    e = evidence("cpu", competing=True)
    with pytest.raises(ValueError, match="evidence_budget_exceeded"):
        build_packet(e, rank_hypotheses(e), max_bytes=500)


@pytest.mark.parametrize(
    "updates",
    [
        {"packet_id": "other-case"},
        {"service_id": "invented"},
        {"supporting_refs": ["fake"]},
        {"contrast_refs": []},
    ],
)
def test_invalid_or_incomplete_references_rejected(updates):
    e = evidence("cpu")
    packet = build_packet(e, rank_hypotheses(e))
    with pytest.raises(ValueError):
        validate_decision(packet, decision(packet, **updates))


def test_ineligible_type_downgrades_but_independent_service_survives():
    e = evidence("mem", specific=True, workload_changed=True)
    packet = build_packet(e, rank_hypotheses(e))
    h = next(h for h in json.loads(packet.text)["hypotheses"] if h["fault"] == "mem")
    result = validate_decision(packet, decision(packet, h))
    assert result["service"] == "a" and result["fault"] == "unknown"
    assert result["status"] == "insufficient_evidence"


def test_unknown_is_valid_and_cannot_be_replaced_by_top_rank():
    e = evidence("cpu")
    packet = build_packet(e, rank_hypotheses(e))
    result = validate_decision(
        packet,
        decision(
            packet,
            service_id=None,
            fault="unknown",
            supporting_refs=[],
            opposing_refs=[],
            contrast_refs=[],
        ),
    )
    assert result["service"] is None and result["fault"] == "unknown"


def test_dense_material_facts_fit_by_lossless_columnar_encoding():
    from app.core.single_pass import EvidenceSet

    e = evidence("cpu")
    copies = []
    for service in ("a", "b", "c"):
        copies.extend(
            f.model_copy(
                update={
                    "service": service,
                    "ref": service + f.ref,
                    "data": {
                        **f.data,
                        "metric": service + "_" + f.family,
                        "before_median": 0.123456789,
                        "after_median": 0.654321987,
                        "ratio": 5.3,
                        "onset_lower": 1234567890,
                        "onset_upper": 1234567891,
                        "post_shape": "sustained",
                        "reset_count": 0,
                        "sampling_gap": False,
                        "scales": {str(s): 0.1 for s in (30, 60, 120, 300)},
                    },
                }
            )
            for f in e.facts
        )
    e = EvidenceSet(
        case_id=e.case_id, window=e.window, services=("a", "b", "c"), facts=tuple(copies)
    )
    hs = rank_hypotheses(e)
    packet = build_packet(e, hs, max_bytes=14000)
    p = packet.payload
    assert "fact_columns" in p
    originals = {f.ref: f for f in e.facts}
    for alias, ref in packet.ref_aliases:
        compact = p["facts"][alias]
        restored = dict(zip(p["fact_columns"][compact["columns"]], compact["values"], strict=True))
        assert restored == originals[ref].data
