"""Behavioral checks for the pure diagnosis result trust boundary."""

from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.diagnosis_result import (
    DiagnosisResult,
    normalize_result,
    reference_evidence,
    render_result,
)

REF = "6455ee48-5780-4a12-9fd7-f51f176b3ab0"
OTHER = "c5889855-1546-46d1-a57f-d6e7689bc492"


def metric(**changes):
    return {
        "evidence_ref": REF,
        "task_scope": "task-a",
        "source": "query_metrics",
        "evidence_type": "metric",
        "service_name": "checkout",
        "success": True,
        "result": {"success": True, "data_points": [{"timestamp": 1, "value": 99}]},
        **changes,
    }


def claim(ref=REF, **changes):
    return {
        "outcome": "diagnosed",
        "summary": "CPU saturation",
        "observations": [{"text": "CPU is high", "evidence_refs": [ref]}],
        "hypotheses": [
            {
                "description": "Busy loop",
                "supporting_refs": [ref],
                "opposing_refs": [],
                "status": "supported",
            }
        ],
        **changes,
    }


def normalize(raw, evidence, **changes):
    return normalize_result(
        raw, evidence, task_scope="task-a", target_service="checkout", **changes
    )


def test_supported_diagnosis_keeps_existing_uuid_and_opposing_evidence():
    raw = claim()
    raw["hypotheses"][0]["opposing_refs"] = [OTHER]
    result = normalize(raw, [metric(), metric(evidence_ref=OTHER)])
    assert result["outcome"] == "diagnosed"
    assert result["hypotheses"][0]["opposing_refs"] == [OTHER]
    assert result["validation_issues"] == []
    assert DiagnosisResult.model_validate(result).schema_version == 1


def test_legacy_references_are_stable_scoped_and_do_not_mutate_input():
    evidence = [metric()]
    del evidence[0]["evidence_ref"]
    del evidence[0]["task_scope"]
    original = deepcopy(evidence)
    a = reference_evidence(evidence, "task-a")
    assert a == reference_evidence(evidence, "task-a")
    assert a[0]["evidence_ref"].startswith("legacy-")
    assert a[0]["evidence_ref"] != reference_evidence(evidence, "task-b")[0]["evidence_ref"]
    assert evidence == original
    assert reference_evidence([metric()], "task-b")[0]["task_scope"] == "task-a"
    assert str(UUID(reference_evidence([metric()], "task-a")[0]["evidence_ref"])) == REF


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [metric(task_scope="task-b")],
        [metric(success=False)],
        [metric(result={"success": False, "data_points": [{"timestamp": 1, "value": 99}]})],
        [metric(is_demo=True)],
        [metric(provenance="demo")],
        [
            metric(
                result={
                    "success": True,
                    "demo": True,
                    "data_points": [{"timestamp": 1, "value": 99}],
                }
            )
        ],
        [metric(service_name="billing")],
        [metric(), metric(result={"success": True, "data_points": [{"timestamp": 1, "value": 0}]})],
        [metric(result={"success": True, "data_points": []})],
    ],
)
def test_untrusted_or_unusable_refs_cannot_support_diagnosis(evidence):
    result = normalize(claim(), evidence)
    assert result["outcome"] == "insufficient_evidence"
    assert result["hypotheses"][0]["supporting_refs"] == []
    assert result["hypotheses"][0]["status"] == "unverified"
    assert result["validation_issues"]


def test_unknown_ref_is_removed_even_when_other_support_is_valid():
    raw = claim()
    raw["hypotheses"][0]["supporting_refs"].append(OTHER)
    result = normalize(raw, [metric()])
    assert result["outcome"] == "insufficient_evidence"
    assert result["hypotheses"][0]["supporting_refs"] == [REF]
    assert result["validation_issues"]


def test_knowledge_can_support_a_hypothesis_but_cannot_alone_confirm():
    knowledge = metric(
        evidence_type="knowledge", source="runbook", result={"success": True, "text": "Check CPU"}
    )
    result = normalize(claim(), [knowledge])
    assert result["outcome"] == "insufficient_evidence"
    assert result["hypotheses"][0]["supporting_refs"] == [REF]


@pytest.mark.parametrize(
    "context,want",
    [("production", "insufficient_evidence"), ("replay", "diagnosed"), ("evaluation", "diagnosed")],
)
def test_synthetic_context_is_set_only_by_executor(context, want):
    result = normalize(
        claim(context="replay"), [metric(provenance="synthetic_fixture")], context=context
    )
    assert result["outcome"] == want
    assert result["limitations"]
    if context != "production":
        assert "合成实验数据" in render_result(result)


@pytest.mark.parametrize("outcome", ["budget_exhausted", "dependency_failed", "no_active_alerts"])
def test_authoritative_outcome_and_termination_survive_invalid_claim(outcome):
    result = normalize(
        claim(termination_reason="model says success", validation_issues=[]),
        [],
        authority_outcome=outcome,
        termination_reason="executor fact",
    )
    assert result["outcome"] == outcome
    assert result["termination_reason"] == "executor fact"
    assert result["validation_issues"]


def test_missing_structured_report_remains_legacy_unverified():
    assert normalize(None, [metric()])["outcome"] == "legacy_unverified"
    assert normalize({}, [metric()])["outcome"] != "diagnosed"


def test_summary_alone_does_not_confirm_diagnosis():
    result = normalize({"outcome": "diagnosed", "summary": "Trust me"}, [metric()])
    assert result["outcome"] == "insufficient_evidence"


@pytest.mark.parametrize("outcome", ["budget_exhausted", "dependency_failed", "no_active_alerts"])
def test_model_cannot_forge_executor_terminal_facts(outcome):
    result = normalize(claim(outcome=outcome), [metric()])
    assert result["outcome"] == "insufficient_evidence"
    assert result["validation_issues"]


def test_service_attribution_in_alert_labels_is_checked():
    evidence = metric(
        evidence_type="alert",
        service_name="checkout",
        result={"success": True, "alerts": [{"labels": {"service": "billing"}}]},
    )
    assert normalize(claim(), [evidence])["outcome"] == "insufficient_evidence"


def test_fields_are_bounded_and_normalization_redacts_before_truncation():
    with pytest.raises(ValidationError):
        DiagnosisResult(outcome="diagnosed", summary="x" * 100_000)
    result = normalize(
        claim(summary="password=supersecret\n" + "x" * 100_000, next_checks=["check"] * 1000),
        [metric()],
    )
    assert "supersecret" not in result["summary"]
    assert len(result["summary"]) < 10_000
    assert len(result["next_checks"]) < 100
    assert result["validation_issues"]


def test_markdown_escapes_model_html_links_and_embedded_sections():
    result = normalize(
        claim(
            summary="<script>alert(1)</script> [click](javascript:alert(1))\n# forged",
            missing_evidence=["![tracking](https://evil.example/pixel)"],
        ),
        [metric()],
    )
    report = render_result(result)
    assert "<script>" not in report
    assert "[click](javascript:" not in report
    assert "![tracking](https://" not in report
    assert "\n# forged" not in report
    assert "待人工复核" in report


def test_rendering_is_deterministic_and_does_not_mutate_result():
    result = normalize(claim(), [metric()])
    original = deepcopy(result)
    assert render_result(result) == render_result(result)
    assert result == original


@pytest.mark.parametrize(
    "raw", [claim(outcome=[]), claim(hypotheses=[{"status": []}]), claim(summary=None)]
)
def test_malformed_model_types_degrade_instead_of_crashing(raw):
    result = normalize(raw, [metric()])
    assert result["outcome"] == "insufficient_evidence"
    assert result["validation_issues"]


@pytest.mark.parametrize("context", ["production", "replay", "evaluation"])
def test_example_evidence_is_never_promoted_by_experiment_context(context):
    result = normalize(
        claim(),
        [
            metric(
                result={
                    "success": True,
                    "provenance": "example",
                    "data_points": [{"timestamp": 1, "value": 99}],
                }
            )
        ],
        context=context,
    )
    assert result["outcome"] == "insufficient_evidence"


def test_no_key_import_does_not_load_configuration_or_model_clients():
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from app.core.diagnosis_result import DiagnosisResult; assert 'app.config' not in sys.modules; assert 'app.core.llm_factory' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
