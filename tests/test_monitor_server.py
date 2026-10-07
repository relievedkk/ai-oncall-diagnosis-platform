from datetime import datetime, timedelta

from mcp_servers import monitor_server


def test_query_service_metric_returns_prometheus_evidence(monkeypatch):
    now = datetime.now().astimezone()
    monkeypatch.setattr(
        monitor_server,
        "_query_range",
        lambda query, start, end, step: [
            {"timestamp": now.isoformat(), "value": 0.1},
            {"timestamp": (now + timedelta(minutes=1)).isoformat(), "value": 0.2},
        ],
    )

    result = monitor_server._query_service_metric(
        "ai-oncall-agent",
        "error_rate",
        None,
        None,
        "1m",
    )

    assert result["success"] is True
    assert result["source"] == "prometheus"
    assert "oncall_http_requests_total" in result["promql"]
    assert result["statistics"]["max"] == 0.2
    assert result["time_range"]["step_seconds"] == 60


def test_query_service_metric_rejects_unknown_metric():
    try:
        monitor_server._query_service_metric("ai-oncall-agent", "random", None, None, "1m")
    except ValueError as exc:
        assert "metric must be one of" in str(exc)
    else:
        raise AssertionError("unknown metrics must be rejected")
