from datetime import datetime, timedelta

import httpx
import pytest

from mcp_servers import monitor_server


@pytest.mark.asyncio
async def test_metric_mcp_schema_includes_only_supported_metrics():
    registered = await monitor_server.mcp.get_tool("query_service_metric")
    allowed = registered.parameters["properties"]["metric"].get("enum", [])
    assert set(allowed) == {"cpu", "memory", "error_rate", "latency_p95", "up", "request_rate"}


@pytest.mark.parametrize("value,status", [(0, "available"), (2.5, "available"), (None, "no_data")])
def test_request_rate_uses_catalog_query_and_preserves_zero_vs_missing(monkeypatch, value, status):
    name = "ai-oncall-agent"
    configured_query = monitor_server.service_catalog.list_services()[name]["metrics"][
        "request_rate"
    ]
    observed = []

    def query(promql, start, end, step):
        observed.append(promql)
        return [] if value is None else [{"timestamp": end.isoformat(), "value": value}]

    monkeypatch.setattr(monitor_server, "_query_range", query)
    result = monitor_server.query_service_metric(name, "request_rate")
    assert result["success"] is True
    assert result["data_status"] == status
    assert result["unit"] == "requests/second"
    assert result["promql"] == configured_query
    assert observed == [configured_query]
    assert len(result["data_points"]) == int(value is not None)
    if value is not None:
        assert result["statistics"]["min"] == value
    else:
        assert result["statistics"] == {}


def test_request_rate_transport_failure_is_not_empty_or_zero_data(monkeypatch):
    def query(*args):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(monitor_server, "_query_range", query)
    result = monitor_server.query_service_metric("ai-oncall-agent", "request_rate")
    assert result["success"] is False
    assert result["data_status"] == "query_failed"
    assert result["data_points"] == []


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


@pytest.mark.parametrize(
    "traffic,rate,status",
    [(0, None, "no_traffic"), (None, None, "no_data"), (3, 0, "available"), (3, 0.1, "available")],
)
def test_error_rate_distinguishes_idle_missing_and_valid_zero(monkeypatch, traffic, rate, status):
    def query(promql, start, end, step):
        value = rate if 'status=~"5.."' in promql else traffic
        return [] if value is None else [{"timestamp": end.isoformat(), "value": value}]

    monkeypatch.setattr(monitor_server, "_query_range", query)
    result = monitor_server._query_service_metric("ai-oncall-agent", "error_rate", None, None, "1m")
    assert result["data_status"] == status
    assert result["success"] is True


def test_error_rate_reports_transport_failure_as_query_failure(monkeypatch):
    def query(*args):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(monitor_server, "_query_range", query)
    result = monitor_server._query_service_metric("ai-oncall-agent", "error_rate", None, None, "1m")
    assert result["data_status"] == "query_failed"
    assert result["success"] is False
