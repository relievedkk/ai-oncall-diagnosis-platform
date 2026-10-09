from app.agent.aiops.executor import classify_tool_evidence


def test_tool_evidence_classification() -> None:
    assert classify_tool_evidence("query_prometheus_alerts") == "alert"
    assert classify_tool_evidence("query_service_metric") == "metric"
    assert classify_tool_evidence("search_log") == "log"
    assert classify_tool_evidence("retrieve_knowledge") == "knowledge"
    assert classify_tool_evidence("custom_probe") == "tool"
