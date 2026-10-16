import json
from types import SimpleNamespace

import pytest

from app.services import health_inspection, rag_agent_service as rag_module


@pytest.mark.parametrize(
    "question",
    [
        "主动检查所有服务的存活状态、CPU、内存、延迟、错误率和近期异常日志",
        "请主动检查全部服务的存活状态、CPU、内存、延迟、错误率和近期异常日志。",
    ],
)
def test_full_health_request_uses_controlled_inspection(question):
    assert health_inspection.is_full_health_request(question)


def test_how_to_question_does_not_execute_health_inspection():
    assert not health_inspection.is_full_health_request(
        "如何实现主动检查所有服务的存活状态、CPU、内存、延迟、错误率和近期异常日志？"
    )


@pytest.mark.asyncio
async def test_chat_returns_controlled_report_and_preserves_history(monkeypatch):
    saved = {}

    class Agent:
        async def aupdate_state(self, config, values, *, as_node):
            saved.update(values)

        async def ainvoke(self, *args, **kwargs):
            raise AssertionError("full health report must not be rewritten by a model")

    async def inspect():
        return "错误率：缺少有效数据，不能判断正常"

    service = rag_module.RagAgentService(streaming=False)
    service.agent = Agent()
    service._agent_initialized = True
    monkeypatch.setattr(rag_module, "inspect_registered_services", inspect)
    question = "主动检查所有服务的存活状态、CPU、内存、延迟、错误率和近期异常日志"
    answer = await service.query(question, "controlled-health-test")
    assert answer == "错误率：缺少有效数据，不能判断正常"
    assert saved["messages"][-1].content == answer
    chunks = [chunk async for chunk in service.query_stream(question, "controlled-health-stream")]
    assert chunks[0]["data"] == answer
    assert chunks[-1]["type"] == "complete"


@pytest.mark.asyncio
async def test_report_never_treats_missing_metrics_or_uningested_logs_as_healthy(monkeypatch):
    monkeypatch.setattr(
        health_inspection, "service_catalog", SimpleNamespace(list_services=lambda: {"orders": {}})
    )
    queries = []

    async def metric(args):
        queries.append(args)
        points = (
            [] if args["metric"] == "error_rate" else [{"value": 1, "timestamp": args["end_time"]}]
        )
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "success": True,
                        "data_points": points,
                        "statistics": {"max": 1},
                        "unit": "ratio",
                        "promql": 'up{job="orders"}',
                    }
                ),
            }
        ]

    async def get_tools(*, server_name):
        return [SimpleNamespace(name="query_service_metric", ainvoke=metric)]

    async def client():
        return SimpleNamespace(get_tools=get_tools)

    async def logs(args):
        return {
            "success": True,
            "returned_count": 0,
            "possibly_truncated": False,
            "query": {"Region": "ap-guangzhou", "TopicId": "orders-topic", "Query": "ERROR"},
        }

    monkeypatch.setattr(health_inspection, "get_mcp_client_with_retry", client)
    monkeypatch.setattr(health_inspection, "query_service_logs", SimpleNamespace(ainvoke=logs))
    report = await health_inspection.inspect_registered_services()

    assert "orders" in report
    assert {q["service_name"] for q in queries} == {"orders"}
    assert {q["metric"] for q in queries} == {"up", "cpu", "memory", "latency_p95", "error_rate"}
    assert len({(q["start_time"], q["end_time"]) for q in queries}) == 1
    assert "错误率 | 缺少有效数据" in report
    assert "不能判断错误率为 0" in report
    assert "不能据此判断应用没有异常" in report
    assert "各项指标都在正常范围" not in report


@pytest.mark.asyncio
async def test_unavailable_monitor_still_returns_log_results(monkeypatch):
    monkeypatch.setattr(
        health_inspection, "service_catalog", SimpleNamespace(list_services=lambda: {"orders": {}})
    )

    async def get_tools(*, server_name):
        raise ConnectionError("monitor unavailable")

    async def client():
        return SimpleNamespace(get_tools=get_tools)

    async def logs(args):
        return {
            "success": True,
            "returned_count": 2,
            "possibly_truncated": True,
            "query": {"Region": "ap-guangzhou", "TopicId": "orders-topic", "Query": "ERROR"},
        }

    monkeypatch.setattr(health_inspection, "get_mcp_client_with_retry", client)
    monkeypatch.setattr(health_inspection, "query_service_logs", SimpleNamespace(ainvoke=logs))
    report = await health_inspection.inspect_registered_services()
    assert "查询失败" in report
    assert "返回 2 条" in report
    assert "可能被截断" in report
