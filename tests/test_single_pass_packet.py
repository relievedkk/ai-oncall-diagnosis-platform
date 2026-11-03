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
