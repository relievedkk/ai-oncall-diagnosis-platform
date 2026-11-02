from app.evaluation.rcaeval_scoring import assess_run, summarize


def sample():
    return {
        "prediction": {"service": "orders", "fault": "cpu"},
        "ground_truth": {"root_cause_service": "orders", "fault": "cpu"},
        "strategy": "agent",
        "errors": [],
        "diagnosis_result": {
            "outcome": "diagnosed",
            "observations": [],
            "hypotheses": [],
            "validation_issues": [],
        },
        "evidence": [],
    }


def test_execution_error_cannot_count_as_success():
    run = sample()
    run["errors"] = ["TimeoutError"]
    result = assess_run(run)
    assert result["timed_out"]
    assert result["label_match"]["joint_correct"]
    assert not result["joint_correct"]


def test_abstention_and_pending_causal_review():
    run = sample()
    run["prediction"]["fault"] = "unknown"
    result = assess_run(run)
    assert result["fault_abstained"] and not result["service_abstained"]
    assert result["causal_support"] is None
    assert result["references_valid"] is None


def test_fabricated_reference_is_detected_even_if_old_flag_is_true():
    run = sample()
    run["valid_references"] = True
    run["diagnosis_result"]["observations"] = [{"evidence_refs": ["fabricated"]}]
    assert assess_run(run)["references_valid"] is False


def test_summary_keeps_failures_in_denominator():
    good, failed = sample(), sample()
    failed["errors"] = ["TimeoutError"]
    result = summarize([good, failed])["agent"]
    assert result["runs"] == 2
    assert result["joint_accuracy"] == 0.5
    assert result["timeout_rate"] == 0.5


def test_protocol_v3_separates_execution_failure_from_safe_unknown():
    unknown, failed = sample(), sample()
    unknown["prediction"] = {"service": "orders", "fault": "unknown"}
    unknown["diagnosis_result"]["outcome"] = "insufficient_evidence"
    failed["errors"] = ["TimeoutError"]

    safe = assess_run(unknown)
    broken = assess_run(failed)

    assert safe["schema_version"] == 3
    assert safe["execution_category"] == "success"
    assert safe["safe_unknown"] is True
    assert broken["execution_category"] == "timeout"
    assert broken["safe_unknown"] is False


def test_protocol_v3_reports_conditional_accuracy_and_candidate_recall():
    good, failed = sample(), sample()
    good["initial_candidates"] = ["orders"]
    good["expanded_candidates"] = ["orders", "payments"]
    good["usage"] = {"model_calls": 2}
    failed["errors"] = ["TimeoutError"]
    failed["prediction"] = {"service": "wrong", "fault": "mem"}
    failed["initial_candidates"] = ["wrong"]
    failed["expanded_candidates"] = ["wrong", "orders"]
    result = summarize([good, failed])["agent"]

    assert result["joint_accuracy"] == 0.5
    assert result["conditional_joint_accuracy"] == 1.0
    assert result["initial_candidate_recall"] == 0.5
    assert result["expanded_candidate_recall"] == 1.0
    assert result["model_calls"] == 2


def test_old_summary_remains_rescorable_without_v3_fields():
    result = assess_run(sample())
    assert result["schema_version"] == 3
    assert result["initial_candidate_recall"] is None
    assert summarize([sample()])["agent"]["conditional_joint_accuracy"] == 1.0
