import json
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.services.rag_agent_service import SummaryMiddleware
from app.services.service_catalog import ServiceCatalog
from app.tools import service_observability
from mcp_servers import monitor_server


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {
                        "display_name": "Orders",
                        "aliases": ["order-api"],
                        "prometheus_job": "orders-job",
                        "metrics": {},
                        "cls": {"region": "ap-guangzhou", "topic_id": "topic-orders"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = ServiceCatalog(str(path))
    monkeypatch.setattr(service_observability, "service_catalog", catalog)
    monkeypatch.setattr(monitor_server, "service_catalog", catalog)
    return catalog


@pytest.mark.asyncio
async def test_summarization_preserves_all_current_health_check_evidence():
    async def summarize(messages):
        return AIMessage(content="Earlier conversation summary")

    messages = [SystemMessage(content="OnCall"), HumanMessage(content="Check all services")]
    metrics = ["up", "cpu", "memory", "latency_p95", "error_rate", "logs"]
    for metric in metrics:
        messages.extend(
            [
                AIMessage(content="", tool_calls=[{"id": metric, "name": metric, "args": {}}]),
                ToolMessage(content=f"Evidence: {metric}", name=metric, tool_call_id=metric),
            ]
        )
    middleware = SummaryMiddleware(SimpleNamespace(ainvoke=summarize))
    update = await middleware.abefore_model({"messages": messages}, None)
    retained = update["messages"] if update else messages
    assert [m.name for m in retained if isinstance(m, ToolMessage)] == metrics


@pytest.mark.asyncio
async def test_summarization_still_compacts_completed_turns():
    async def summarize(messages):
        return AIMessage(content="Earlier conversation summary")

    messages = [SystemMessage(content="OnCall")]
    for index in range(6):
        messages.extend(
            [HumanMessage(content=f"Earlier question {index}"), AIMessage(content="Done")]
        )
    messages.extend([HumanMessage(content="Current check"), AIMessage(content="Checking")])
    middleware = SummaryMiddleware(SimpleNamespace(ainvoke=summarize))
    update = await middleware.abefore_model({"messages": messages}, None)
    assert update is not None
    retained = [m for m in update["messages"] if isinstance(m, HumanMessage)]
    assert retained[-1].content == "Current check"
    assert len(retained) < 7


def test_inventory_exposes_configured_services_and_scope(catalog):
    result = service_observability.list_monitored_services.invoke({})
    assert result["services"][0]["service_name"] == "orders"
    assert result["services"][0]["prometheus_job"] == "orders-job"
    assert result["services"][0]["cls"]["topic_id"] == "topic-orders"
    assert result["scope"] == "registered_application_services"


def test_every_catalog_metric_can_be_queried_through_monitor_tool(monkeypatch):
    actual_catalog = ServiceCatalog("config/service_catalog.json")
    monkeypatch.setattr(service_observability, "service_catalog", actual_catalog)
    monkeypatch.setattr(monitor_server, "service_catalog", actual_catalog)
    monkeypatch.setattr(monitor_server, "_query_range", lambda *args: [])
    inventory = service_observability.list_monitored_services.invoke({})
    queried = set()
    for service in inventory["services"]:
        for metric, promql in service["metrics"].items():
            result = monitor_server.query_service_metric(service["service_name"], metric)
            assert result["success"] is True
            assert result["data_status"] == "no_data"
            assert result["promql"] == promql
            queried.add(metric)
    assert "request_rate" in queried


@pytest.mark.parametrize("name", ["all", "service-a", "not-registered"])
def test_monitor_rejects_unknown_service_instead_of_querying_fake_job(catalog, name):
    with pytest.raises(ValueError, match="list_monitored_services"):
        monitor_server._query_service_metric(name, "up", None, None, "1m")


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["ERROR", "FATAL", "CRITICAL"])
async def test_log_query_resolves_topic_and_numeric_time_before_calling_search(
    catalog, monkeypatch, severity
):
    observed = {}

    async def search(args):
        observed.update(args)
        return [
            {
                "type": "text",
                "text": json.dumps(
                    [
                        {
                            "LogJson": json.dumps(
                                {"service": "orders", "level": severity, "message": "failed"},
                                separators=(",", ":"),
                            )
                        }
                    ]
                ),
            }
        ]

    async def get_tools(*, server_name):
        assert server_name == "cls"
        return [SimpleNamespace(name="SearchLog", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke(
        {"service_name": "order-api", "lookback_minutes": 15}
    )
    assert result["success"] is True
    assert result["service_name"] == "orders"
    assert result["returned_count"] == 1
    assert result["records"][0]["LogJson"].endswith('"failed"}')
    assert json.loads(result["records"][0]["LogJson"])["level"] == severity
    assert observed["TopicId"] == "topic-orders"
    assert observed["Region"] == "ap-guangzhou"
    assert isinstance(observed["From"], int)
    assert observed["To"] - observed["From"] == 900_000
    assert abs(observed["To"] - int(time.time() * 1000)) < 5000
    # Full-text ERROR also matches INFO messages describing error handling.
    # The server-side predicate must constrain the structured severity field.
    assert observed["Query"] == (
        'service:"orders" AND (level:"ERROR" OR level:"FATAL" OR level:"CRITICAL")'
    )


@pytest.mark.asyncio
async def test_log_query_reports_missing_mapping_without_guessing_topic(catalog):
    result = await service_observability.query_service_logs.ainvoke({"service_name": "all"})
    assert result["success"] is False
    assert "list_monitored_services" in result["error"]


@pytest.mark.asyncio
async def test_log_query_does_not_report_tool_error_as_zero_logs(catalog, monkeypatch):
    async def search(args):
        return [{"type": "text", "text": "MCP error -32602: Invalid arguments"}]

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="SearchLog", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke({"service_name": "orders"})
    assert result["success"] is False
    assert "returned_count" not in result


@pytest.mark.asyncio
async def test_log_query_rejects_missing_response_instead_of_claiming_no_errors(
    catalog, monkeypatch
):
    async def search(args):
        return []

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="SearchLog", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke({"service_name": "orders"})
    assert result["success"] is False
    assert "returned_count" not in result


@pytest.mark.asyncio
async def test_local_cls_adapter_uses_local_arguments_and_marks_demo(catalog, monkeypatch):
    observed = {}

    async def search(args):
        observed.update(args)
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "success": True,
                        "is_demo": True,
                        "logs": [
                            {"service": "orders", "level": "ERROR", "message": "demo failure"},
                            {"service": "orders", "level": "INFO", "message": "ERROR handling"},
                        ],
                    }
                ),
            }
        ]

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="search_log", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke({"service_name": "orders"})
    assert result["success"] is True
    assert result["is_demo"] is True
    assert result["source"] == "demo.cls.search_log"
    assert result["returned_count"] == 1
    assert result["records"][0]["level"] == "ERROR"
    assert observed["topic_id"] == "topic-orders"
    assert observed["end_time"] - observed["start_time"] == 900_000
    assert "Region" not in observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,success",
    [
        ({"success": False, "error": "topic does not exist", "logs": []}, False),
        ({"success": True, "logs": []}, True),
        ({"success": True}, False),
    ],
)
async def test_local_cls_distinguishes_errors_missing_payload_and_empty_logs(
    catalog, monkeypatch, payload, success
):
    async def search(args):
        return [{"type": "text", "text": json.dumps(payload)}]

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="search_log", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke({"service_name": "orders"})
    assert result["success"] is success
    if success:
        assert result["returned_count"] == 0
        assert result["is_demo"] is True
    else:
        assert "returned_count" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,success",
    [
        ({"Response": {"Results": []}}, True),
        ({"Response": {"Error": {"Code": "AuthFailure", "Message": "denied"}}}, False),
        ({"Response": {"Results": ["malformed"]}}, False),
    ],
)
async def test_official_cls_response_envelope(catalog, monkeypatch, payload, success):
    async def search(args):
        return [{"type": "text", "text": json.dumps(payload)}]

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="SearchLog", ainvoke=search)]

    async def get_client():
        return SimpleNamespace(get_tools=get_tools)

    monkeypatch.setattr(service_observability, "get_mcp_client_with_retry", get_client)
    result = await service_observability.query_service_logs.ainvoke({"service_name": "orders"})
    assert result["success"] is success
    if success:
        assert result["returned_count"] == 0
        assert result["is_demo"] is False
