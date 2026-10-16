import json

import pytest

from app.evaluation.diagnosis import DiagnosisCase, evaluate_diagnosis, evaluate_suite

REPORT = "时间范围 10:00-10:05，5xx 错误率关键数值为 12%。处理建议：回滚；恢复验证：低于 1%。"


def _evidence():
    return [
        {
            "evidence_type": "metric",
            "source": "prometheus",
            "success": True,
            "payload": {"data_points": [{"timestamp": "2026-10-15T10:00:00Z", "value": 0.12}]},
        },
        {
            "evidence_type": "log",
            "source": "cls.SearchLog",
            "success": True,
            "payload": {"records": [{"level": "ERROR", "message": "HTTP 503"}]},
        },
    ]


def _case() -> DiagnosisCase:
    return DiagnosisCase(
        id="http-errors",
        alert_name="HighHTTPErrorRate",
        root_cause_term_groups=[["5xx", "错误率"]],
        required_evidence_types=["metric", "log"],
        required_report_terms=["时间范围", "关键数值", "处理建议", "恢复验证"],
        forbidden_terms=["无证据结论"],
    )


def test_complete_grounded_diagnosis_passes() -> None:
    result = evaluate_diagnosis(
        _case(),
        REPORT,
        _evidence(),
    )

    assert result.passed is True
    assert result.score == 1.0


def test_unsupported_and_incomplete_diagnosis_fails() -> None:
    result = evaluate_diagnosis(
        _case(),
        "5xx 错误率升高，这是无证据结论。",
        _evidence()[:1],
    )

    assert result.passed is False
    assert result.groundedness_score == 0.0
    assert result.missing_evidence_types == ["log"]
    assert "时间范围" in result.missing_report_terms


def test_suite_reports_regression() -> None:
    summary = evaluate_suite(
        [_case()],
        {"http-errors": {"report": "empty", "evidence": []}},
    )

    assert summary["passed"] is False
    assert summary["pass_rate"] == 0.0


@pytest.mark.parametrize(
    "invalid",
    [
        [{"evidence_type": "metric"}, {"evidence_type": "log"}],
        [{"evidence_type": kind, "source": "tool", "success": False} for kind in ["metric", "log"]],
        [
            {"evidence_type": kind, "source": "tool", "success": True, "payload": {}}
            for kind in ["metric", "log"]
        ],
    ],
)
def test_keyword_report_cannot_pass_with_failed_or_missing_evidence(invalid):
    result = evaluate_diagnosis(_case(), REPORT, invalid)
    assert result.passed is False
    assert result.evidence_score == 0
    assert result.missing_evidence_types == ["metric", "log"]


def test_nested_failed_tool_response_is_not_evidence():
    evidence = _evidence()
    evidence[0]["payload"]["success"] = False
    result = evaluate_diagnosis(_case(), REPORT, evidence)
    assert result.passed is False
    assert result.missing_evidence_types == ["metric"]


def test_missing_log_records_fail_gate_even_when_weighted_score_is_high():
    evidence = _evidence()
    evidence[1]["payload"]["records"] = []
    result = evaluate_diagnosis(_case(), REPORT, evidence)
    assert result.passed is False
    assert result.missing_evidence_types == ["log"]


def test_gate_reports_that_keywords_do_not_verify_semantic_accuracy():
    result = evaluate_diagnosis(_case(), REPORT, _evidence()).to_dict()
    assert result["semantic_accuracy_verified"] is False
    assert result["evaluation_method"] == "keyword_and_evidence_gate"


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "is_demo": True, "records": [{"message": "fake"}]},
        {"success": True, "records": [{"unrelated": "not log data"}]},
    ],
)
def test_demo_or_malformed_logs_do_not_satisfy_evidence_requirement(payload):
    evidence = _evidence()
    evidence[1]["payload"] = payload
    assert evaluate_diagnosis(_case(), REPORT, evidence).passed is False


def test_failed_payload_wrapper_cannot_be_overridden_by_successful_nested_result():
    evidence = _evidence()
    evidence[0]["payload"] = {
        "success": False,
        "result": {"success": True, **evidence[0]["payload"]},
    }
    assert evaluate_diagnosis(_case(), REPORT, evidence).passed is False


def test_executor_mcp_text_block_results_are_valid_evidence():
    evidence = _evidence()
    for item in evidence:
        item["result"] = [
            {"type": "text", "text": json.dumps({"success": True, **item.pop("payload")})}
        ]
        item.pop("success")
    assert evaluate_diagnosis(_case(), REPORT, evidence).passed is True
