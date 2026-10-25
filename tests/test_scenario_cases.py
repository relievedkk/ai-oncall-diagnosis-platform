"""Synthetic inputs must stay separate from scoring labels."""

import json

import pytest


def test_inputs_cover_twelve_distinct_cases_without_private_expectations():
    from app.evaluation.scenarios import load_case, load_cases

    cases = load_cases()
    assert len(cases) == 12
    assert len({case["case_id"] for case in cases}) == 12
    assert {
        "cpu_load",
        "cpu_spin",
        "missing_logs",
        "conflicting_signals",
        "dependency_failure",
    } <= {case["case_id"] for case in cases}
    for case in cases:
        assert case["source"] == "synthetic"
        assert case["target_service"] == "checkout-api"
        serialized = json.dumps(case)
        assert "acceptable_outcomes" not in serialized
        assert "known_cause" not in serialized
        assert "review_status" not in serialized
        assert load_case(case["case_id"]) == case
    cases[0]["task"] = "mutated"
    assert load_cases()[0]["task"] != "mutated"


@pytest.mark.parametrize("case_id", ["../expectations", "https://example.com", "", "unknown"])
def test_case_loader_rejects_arbitrary_paths_and_unknown_cases(case_id):
    from app.evaluation.scenarios import load_case

    with pytest.raises(ValueError, match="Unknown case"):
        load_case(case_id)


def test_normal_fixtures_are_usable_by_production_evidence_validator():
    from app.core.evidence import valid_evidence
    from app.evaluation.scenarios import load_case

    case = load_case("cpu_load")
    for name, evidence_type in (
        ("query_service_metrics", "metric"),
        ("query_service_logs", "log"),
        ("query_prometheus_alerts", "alert"),
    ):
        assert valid_evidence(
            {"source": name, "evidence_type": evidence_type, "result": case["tools"][name]}
        )


def test_failure_and_empty_log_fixtures_cannot_support_observations():
    from app.core.evidence import valid_evidence
    from app.evaluation.scenarios import load_case

    for case_id, name, evidence_type in (
        ("missing_logs", "query_service_logs", "log"),
        ("dependency_failure", "query_service_metrics", "metric"),
        ("dependency_failure", "query_service_logs", "log"),
    ):
        case = load_case(case_id)
        assert not valid_evidence(
            {"source": name, "evidence_type": evidence_type, "result": case["tools"][name]}
        )
