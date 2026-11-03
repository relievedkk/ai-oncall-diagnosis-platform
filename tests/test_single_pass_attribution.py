from app.evaluation.single_pass_hypotheses import rank_hypotheses
from tests.test_single_pass_hypotheses import evidence


def attribute(e, fault="cpu", *, selected=None, packet_error=None):
    from app.evaluation.single_pass_attribution import attribute_failure

    catalog = rank_hypotheses(e, limit=6)
    return attribute_failure(
        e,
        rank_hypotheses(e) if selected is None else selected,
        catalog=catalog,
        truth={"root_cause_service": "a", "fault": fault},
        packet_error=packet_error,
    )


def test_attribution_separates_retrieval_budget_and_permission():
    e = evidence("cpu")
    result = attribute(e, selected=(), packet_error="evidence_budget_exceeded")
    assert result["axes"]["retrieval"] == "missing_top5"
    assert result["axes"]["packet"] == "evidence_budget_exceeded"
    assert result["axes"]["permission"] == "eligible"
    assert "retrieval_miss" in result["categories"] and "packet_budget" in result["categories"]
    assert result["supporting_refs"]


def test_unextracted_loss_feature_does_not_claim_raw_data_absence():
    e = evidence("cpu")
    e = e.model_copy(update={"facts": tuple(f for f in e.facts if f.family != "loss")})
    result = attribute(e, "loss")
    assert result["raw_data_absence_proven"] is False
    assert result["raw_review_required"] is True
    assert "extracted_family_absent" in result["categories"]
    assert "review_original_mechanism_sources" in result["actions"]


def test_competition_and_unknown_metadata_are_independent_reasons():
    result = attribute(evidence("cpu", known=False, competing=True))
    assert "unverified_measurement_semantics" in result["categories"]
    assert "unresolved_competition" in result["categories"]
    assert result["opposing_refs"]


def test_missing_baseline_has_own_follow_up_even_when_other_facts_exist():
    e = evidence("disk")
    e = e.model_copy(
        update={
            "facts": tuple(
                f.model_copy(
                    update={
                        "data": {
                            **f.data,
                            "coverage": "baseline_missing",
                            "changed": False,
                            "before_samples": 0,
                        }
                    }
                )
                if f.family == "disk"
                else f
                for f in e.facts
            )
        }
    )
    result = attribute(e, "disk")
    assert "baseline_gap" in result["categories"]
    assert "verify_baseline_coverage" in result["actions"]


def test_human_report_preserves_failed_cases_and_reviewable_refs(tmp_path):
    from app.evaluation.single_pass_attribution import write_attribution_report

    e = evidence("cpu")
    audit = {
        "cases": [
            {"case_id": "case-013", "audited": True, "attribution": attribute(e)},
            {"case_id": "case-022", "audited": False, "error": "case_audit_failed"},
        ]
    }
    path = tmp_path / "failure-attribution.md"
    write_attribution_report(audit, path)
    text = path.read_text(encoding="utf-8")
    assert "case-013" in text and "case-022" in text
    assert e.facts[0].ref in text
    assert "case_audit_failed" in text
