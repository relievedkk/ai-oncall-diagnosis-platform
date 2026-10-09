from app.evaluation.diagnosis import DiagnosisCase, evaluate_diagnosis, evaluate_suite


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
        "时间范围 10:00-10:05，5xx 错误率关键数值为 12%。处理建议：回滚；恢复验证：低于 1%。",
        [{"evidence_type": "metric"}, {"evidence_type": "log"}],
    )

    assert result.passed is True
    assert result.score == 1.0


def test_unsupported_and_incomplete_diagnosis_fails() -> None:
    result = evaluate_diagnosis(
        _case(),
        "5xx 错误率升高，这是无证据结论。",
        [{"evidence_type": "metric"}],
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
