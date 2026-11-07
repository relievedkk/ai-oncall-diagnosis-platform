import json

import pytest
from pydantic import ValidationError

from app.agent.aiops import single_pass as engine
from app.core.single_pass import canonical
from app.evaluation.single_pass_hypotheses import rank_hypotheses
from app.evaluation.single_pass_packet import build_packet
from tests.test_single_pass_execution import Model
from tests.test_single_pass_hypotheses import evidence


def choice(packet, **changes):
    assert hasattr(engine, "SinglePassChoice")
    h = next(h for h in packet.payload["hypotheses"] if h["eligible"])
    return engine.SinglePassChoice(
        **{"hypothesis_id": h["id"], "decision": "candidate", "reason": "brief", **changes}
    )


def packet():
    e = evidence("cpu")
    return build_packet(e, rank_hypotheses(e))


def test_choice_server_binds_material_refs_and_preserves_proposal():
    p = packet()
    result = engine.validate_choice(p, choice(p))
    assert result["status"] == "supported" and result["service"] == "a" and result["fault"] == "cpu"
    assert result["proposal"]["hypothesis_id"] == p.payload["hypotheses"][0]["id"]
    assert result["proposal"]["service"] == "a"
    assert result["supporting_refs"] and result["contrast_refs"]
    assert result["evidence_binding_origin"] == "server_bound_candidate"
    assert result["downgrade_reasons"] == []
    assert len(canonical(choice(p).model_dump()).encode()) < 256


def test_invalid_choice_id_is_rejected_not_repaired():
    p = packet()
    with pytest.raises(ValueError, match="unknown_hypothesis_id"):
        engine.validate_choice(p, choice(p, hypothesis_id="SECRET"))


def test_service_only_keeps_localization_without_asserting_cause():
    p = packet()
    result = engine.validate_choice(p, choice(p, decision="service_only"))
    assert result["service"] == "a" and result["fault"] == "unknown"
    assert not result["confirmed"] and result["candidate_fault"] == "unknown"


def test_unknown_does_not_get_automatic_top_rank_fallback():
    p = packet()
    result = engine.validate_choice(p, choice(p, decision="unknown", hypothesis_id=None))
    assert result["service"] is None and result["candidate_service"] is None
    assert result["fault"] == result["candidate_fault"] == "unknown"


def test_readable_candidate_records_are_not_positional_arrays():
    p = packet()
    wire = json.loads(p.text)
    assert isinstance(wire["hypotheses"][0], dict)
    assert wire["hypotheses"][0]["id"] == "h1"


def test_server_bound_evidence_is_not_limited_by_the_old_model_copy_array_limit():
    from app.core.single_pass import fact_for

    e = evidence("cpu")
    added = tuple(
        fact_for(
            e.case_id,
            e.window,
            "a",
            "metrics",
            "disk",
            {
                "coverage": "complete",
                "changed": False,
                "unit": "count",
                "metric_kind": "gauge",
                "index": i,
            },
        )
        for i in range(35)
    )
    e = e.model_copy(update={"facts": e.facts + added})
    p = build_packet(e, rank_hypotheses(e))
    result = engine.validate_choice(p, choice(p))
    assert result["status"] == "supported" and len(result["contrast_refs"]) > 32


@pytest.mark.asyncio
async def test_invalid_choice_keeps_usage_and_value_free_detail(tmp_path):
    from app.evaluation.single_pass_journal import AttemptJournal

    p = packet()
    c = choice(p, hypothesis_id="SECRET")
    model = Model(c)
    r = await engine.run_single_pass(
        p,
        journal=AttemptJournal(tmp_path / "attempts.sqlite"),
        experiment_id="bad-choice",
        config={},
        model_factory=lambda: model,
    )
    assert r["status"] == "error" and r["failure_detail_code"] == "unknown_hypothesis_id"
    assert r["usage"]["total_tokens"] == 25 and model.calls == 1
    assert "SECRET" not in json.dumps(r)


@pytest.mark.asyncio
async def test_schema_issue_preserves_safe_field_path_without_value(tmp_path):
    from app.evaluation.single_pass_journal import AttemptJournal

    p = packet()
    choice(p)
    try:
        engine.SinglePassChoice.model_validate({"decision": "SECRET", "hypothesis_id": None})
    except ValidationError as exc:
        error = exc

    class InvalidModel(Model):
        async def ainvoke(self, messages):
            response = await super().ainvoke(messages)
            response["parsing_error"] = error
            return response

    r = await engine.run_single_pass(
        p,
        journal=AttemptJournal(tmp_path / "j.sqlite"),
        experiment_id="schema",
        config={},
        model_factory=lambda: InvalidModel(),
    )
    assert r["failure_detail_code"] == "schema_parse_failed"
    assert {"code": "invalid_enum", "path": "decision"} in r["schema_issues"]
    assert "SECRET" not in json.dumps(r)
