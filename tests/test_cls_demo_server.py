from mcp_servers import cls_server


def test_demo_logs_explicitly_identify_synthetic_source():
    result = cls_server.search_log("topic-001", 1_000, 61_000)
    assert result["success"] is True
    assert result["is_demo"] is True
    assert result["source"] == "demo.cls.search_log"
    assert result["logs"][0]["service"] == "data-sync-service"


def test_demo_logs_honor_supported_structured_service_severity_query():
    result = cls_server.search_log(
        "topic-001",
        1_000,
        61_000,
        'service:"data-sync-service" AND (level:"ERROR" OR level:"FATAL" OR level:"CRITICAL")',
    )
    assert result["success"] is True
    assert result["logs"] == []


def test_demo_logs_unknown_topic_is_failure():
    result = cls_server.search_log("unknown", 1_000, 61_000)
    assert result["success"] is False
    assert result["is_demo"] is True


def test_demo_logs_cannot_claim_unsupported_query_succeeded():
    result = cls_server.search_log("topic-001", 1_000, 61_000, 'message:"failure"')
    assert result["success"] is False
    assert result["error"]
