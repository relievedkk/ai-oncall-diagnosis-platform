"""Diagnosis scoring and remediation must reject the same unusable evidence."""

import json

import pytest

from app.evaluation.diagnosis import DiagnosisCase, evaluate_diagnosis
from app.services.diagnosis_runner import _decode_evidence_result, _remediation_evidence


def payload(kind):
    if kind == "metric":
        return {
            "success": True,
            "data_points": [{"timestamp": "2026-10-16T00:00:00Z", "value": 90}],
        }
    return {"success": True, "logs": [{"level": "ERROR", "message": "HTTP 503"}]}


def accepted(item, gate):
    if gate == "remediation":
        return _remediation_evidence({"evidence": [item]})[0]["success"]
    case = DiagnosisCase("evidence", "Alert", [["failure"]], [item["evidence_type"]], [])
    return evaluate_diagnosis(case, "failure", [item]).passed


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
@pytest.mark.parametrize("kind", ["metric", "log"])
@pytest.mark.parametrize(
    "marker,value",
    [
        ("isError", True),
        ("is_demo", True),
        ("demo", True),
        ("error", "query unavailable"),
        ("success", False),
    ],
)
@pytest.mark.parametrize("layer", ["item", "payload", "result", "text", "content"])
def test_failure_markers_survive_every_tool_response_wrapper(gate, kind, marker, value, layer):
    result = payload(kind)
    item = {"evidence_type": kind, "source": "real-monitor", "payload": {"result": result}}
    if layer == "item":
        item[marker] = value
    elif layer == "payload":
        item["payload"][marker] = value
    elif layer == "result":
        result[marker] = value
    elif layer == "text":
        item["payload"]["result"] = [{"type": "text", "text": json.dumps(result), marker: value}]
    else:
        item["payload"]["result"] = {
            "content": [{"type": "text", "text": json.dumps(result)}],
            marker: value,
        }
    assert accepted(item, gate) is False


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
@pytest.mark.parametrize(
    "point",
    [
        {"value": 90},
        {"timestamp": "not-a-time", "value": 90},
        {"timestamp": float("inf"), "value": 90},
        {"timestamp": True, "value": 90},
        {"timestamp": "2026-10-16T00:00:00Z", "value": float("nan")},
        {"timestamp": "2026-10-16T00:00:00Z", "value": float("inf")},
        {"timestamp": "2026-10-16T00:00:00Z", "value": True},
    ],
)
def test_metrics_require_actual_timestamp_and_finite_numeric_value(gate, point):
    item = {
        "evidence_type": "metric",
        "source": "prometheus",
        "payload": {
            "result": {"success": True, "data_points": [point]},
        },
    }
    assert accepted(item, gate) is False


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
@pytest.mark.parametrize("records", [[], [{}], [{"message": " "}], [{"LogJson": "invalid"}]])
def test_log_evidence_requires_nonempty_actual_records(gate, records):
    item = {
        "evidence_type": "log",
        "source": "cls",
        "payload": {
            "result": {"success": True, "logs": records},
        },
    }
    assert accepted(item, gate) is False


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
@pytest.mark.parametrize("kind", ["metric", "log"])
def test_valid_mcp_observations_pass_both_gates(gate, kind):
    item = {
        "evidence_type": kind,
        "source": "real-monitor",
        "payload": {
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(payload(kind))}],
            }
        },
    }
    assert accepted(item, gate) is True


def test_decoder_preserves_outer_error_provenance():
    decoded = _decode_evidence_result(
        {
            "isError": True,
            "content": [
                {"type": "text", "text": json.dumps(payload("metric"))},
            ],
        }
    )
    assert decoded["isError"] is True
    assert decoded["success"] is False


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
def test_mixed_mcp_blocks_cannot_hide_a_failed_block(gate):
    item = {
        "evidence_type": "metric",
        "source": "prometheus",
        "payload": {
            "result": [
                {"type": "text", "text": json.dumps(payload("metric"))},
                {"type": "text", "text": '{"success": false, "error": "partial failure"}'},
            ]
        },
    }
    assert accepted(item, gate) is False


@pytest.mark.parametrize("gate", ["evaluation", "remediation"])
def test_malformed_status_is_rejected_without_crashing(gate):
    item = {
        "evidence_type": "metric",
        "source": "prometheus",
        "payload": {
            "result": {**payload("metric"), "data_status": ["available"]},
        },
    }
    assert accepted(item, gate) is False
