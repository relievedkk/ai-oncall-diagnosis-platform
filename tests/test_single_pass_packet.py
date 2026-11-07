import json

import pytest

from app.agent.aiops.single_pass import (
    SinglePassChoice,
    SinglePassDecision,
    render_report,
    validate_decision,
)
from app.evaluation.single_pass_hypotheses import rank_hypotheses
from app.evaluation.single_pass_packet import build_packet
from tests.test_single_pass_hypotheses import evidence


def decision(packet, h=None, **updates):
    payload = packet.payload
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
    data = packet.payload
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
    h = next(h for h in packet.payload["hypotheses"] if h["fault"] == "mem")
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


def test_dense_full_precision_material_fits_without_eviction_and_decodes_literals():
    from app.core.single_pass import EvidenceSet

    seed = evidence("cpu")
    services = tuple("long-service-identifier-" + str(i) for i in range(5))
    facts = []
    for service in services:
        for f in seed.facts:
            data = {
                **f.data,
                "metric": service + "_" + f.family,
                "before_median": 0.12345678912345678,
                "after_median": 0.654321987654321,
                "scales": {str(t): 0.654321987654321 for t in (30, 60, 120, 300)},
                "semantic_source": "verified-source-with-a-long-version-and-collection-definition-reference",
                "literal": {"$v": 0, "nested": [{"$literal": {"$v": 2}}]},
            }
            if not f.data["changed"]:
                data["after_median"] = data["before_median"]
                data["scales"] = {str(t): data["before_median"] for t in (30, 60, 120, 300)}
            facts.append(
                f.model_copy(update={"service": service, "ref": service + f.ref, "data": data})
            )
    e = EvidenceSet(case_id=seed.case_id, window=seed.window, services=services, facts=tuple(facts))
    hs = rank_hypotheses(e)
    packet = build_packet(e, hs)
    p = packet.payload
    assert packet.transport_bytes <= 16384
    assert "shared_values" in json.loads(packet.text)
    originals = {f.ref: f for f in e.facts}
    required = {ref for h in hs for ref in h.supporting_refs + h.opposing_refs + h.contrast_refs}
    assert set(dict(packet.ref_aliases).values()) == required
    for alias, ref in packet.ref_aliases:
        fact = p["facts"][alias]
        restored = dict(zip(p["fact_columns"][fact["columns"]], fact["values"], strict=True))
        assert restored == originals[ref].data
    assert validate_decision(packet, decision(packet))["fault"] == "cpu"


@pytest.mark.parametrize("case_id", ["case-030", "case-041"])
def test_large_development_packets_keep_all_material_values_without_reading_labels(case_id):
    from pathlib import Path

    from app.evaluation.rcaeval import Pilot
    from app.evaluation.single_pass_hypotheses import collect_evidence

    root = Path(".runtime/rcaeval/heldout-18-v1/tasks")
    if not (root / case_id).is_dir():
        pytest.skip("optional recorded development telemetry is not installed")
    view = Pilot(root).open(case_id)
    e = collect_evidence(view, telemetry_profile="rcaeval-re2")
    hs = rank_hypotheses(e)
    packet = build_packet(e, hs)
    assert packet.transport_bytes <= 16384
    original = {f.ref: f for f in e.facts}
    required = {r for h in hs for r in h.supporting_refs + h.opposing_refs + h.contrast_refs}
    assert set(dict(packet.ref_aliases).values()) == required
    payload = packet.payload
    for alias, ref in packet.ref_aliases:
        f = payload["facts"][alias]
        restored = dict(zip(payload["fact_columns"][f["columns"]], f["values"], strict=True))
        for key, value in original[ref].data.items():
            if key not in {"excerpt", "template", "scales"}:
                assert restored[key] == value
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.core.llm_factory import SafeChatOpenAI
    from app.core.single_pass import canonical
    from app.evaluation.single_pass_packet import OUTPUT_ALLOWANCE, SYSTEM_PROMPT

    model = SafeChatOpenAI(
        model="qwen-max", api_key="test-placeholder", temperature=0, streaming=False, max_tokens=384
    )
    request = model._get_request_payload(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=packet.text)],
        tools=[convert_to_openai_tool(SinglePassChoice)],
        tool_choice={"type": "function", "function": {"name": "SinglePassChoice"}},
        parallel_tool_calls=False,
    )
    assert len(canonical(request).encode()) + OUTPUT_ALLOWANCE <= 16384
    assert len(canonical(request).encode()) + OUTPUT_ALLOWANCE <= packet.transport_bytes


def test_emitted_pool_has_unambiguous_literal_semantics_for_an_independent_consumer():
    seed = evidence("cpu")
    literal = {"data": "X" * 100, "reserved": {"$v": 0}, "nested": {"$literal": {"$v": 2}}}
    e = seed.model_copy(
        update={
            "facts": tuple(
                f.model_copy(update={"data": {**f.data, "literal": literal}}) for f in seed.facts
            )
        }
    )
    raw = json.loads(build_packet(e, rank_hypotheses(e)).text)
    pool = raw["shared_values"]

    def consume(node, depth=0):
        if depth > 20:
            raise ValueError("cyclic_reference_in_emitted_protocol")
        if isinstance(node, dict):
            if set(node) == {"$literal"}:
                return node["$literal"]  # Whole subtree is literal; no nested expansion.
            if set(node) == {"$v"}:
                return consume(pool[node["$v"]], depth + 1)
            return {k: consume(v, depth + 1) for k, v in node.items()}
        if isinstance(node, list):
            return [consume(v, depth + 1) for v in node]
        return node

    for row in raw["facts"].values():
        record = dict(zip(raw["fact_record_fields"], consume(row), strict=True))
        columns = consume(raw["fact_columns"][record["columns"]])
        assert record["values"][columns.index("literal")] == literal
