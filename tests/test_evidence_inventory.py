import json

from app.evaluation.single_pass_hypotheses import collect_evidence
from tests.single_pass_helpers import view_of


def test_inventory_separates_raw_absence_from_extractor_loss_and_unknown_units():
    from app.evaluation.evidence_inventory import inventory_case

    view = view_of({"orders_cpu": [1] * 20, "orders_diskio": [2] * 20})
    evidence = collect_evidence(view)
    partial = evidence.model_copy(
        update={"facts": tuple(f for f in evidence.facts if f.family != "disk")}
    )
    result = inventory_case(view, partial)
    families = result["services"]["orders"]
    assert families["disk"]["status"] == "not_extracted"
    assert families["cpu"]["status"] == "semantics_unverified"
    assert families["loss"]["status"] == "not_collected"
    assert families["loss"]["scope"] == "metrics_schema_only"
    assert "orders_diskio" in result["unextracted_metric_columns"]


def test_inventory_retains_optional_source_absence_and_coverage_without_log_text():
    from app.evaluation.evidence_inventory import inventory_case

    view = view_of(
        {"orders_mem": [1] * 20},
        logs=[{"timestamp": 12, "container_name": "orders", "message": "private context ERROR"}],
    )
    result = inventory_case(view, collect_evidence(view, telemetry_profile="rcaeval-re2"))
    assert result["sources"]["traces"]["status"] == "not_available"
    assert result["sources"]["logs"]["processed_rows"] == 1
    assert result["services"]["orders"]["mem"]["status"] == "available"
    assert "private context" not in json.dumps(result)
    assert result["missing_direct_metric_fields"]["disk"]
    assert result["missing_direct_metric_fields"]["loss"]


def test_inventory_does_not_treat_missing_baseline_or_diskio_as_direct_mechanism():
    from app.evaluation.evidence_inventory import inventory_case

    view = view_of({"orders_diskio": [None] * 10 + [5] * 10})
    result = inventory_case(view, collect_evidence(view))
    assert result["services"]["orders"]["disk"]["status"] == "insufficient_coverage"
    assert result["missing_direct_metric_fields"]["disk"]
    assert result["mechanism_confirmed"] is False
