import importlib
import json
from typing import TypedDict

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph

from app.agent.aiops.executor import classify_tool_evidence


def test_tool_evidence_classification() -> None:
    assert classify_tool_evidence("query_prometheus_alerts") == "alert"
    assert classify_tool_evidence("query_service_metric") == "metric"
    assert classify_tool_evidence("search_log") == "log"
    assert classify_tool_evidence("retrieve_knowledge") == "knowledge"
    assert classify_tool_evidence("custom_probe") == "tool"


async def setup_executor(monkeypatch, model, tools):
    module = importlib.import_module("app.agent.aiops.executor")

    async def get_client():
        return None

    async def get_tools(_client):
        return [], None

    monkeypatch.setattr(module, "get_mcp_client_with_retry", get_client)
    monkeypatch.setattr(module, "load_mcp_tools_safe", get_tools)
    monkeypatch.setattr(module, "DEFAULT_LOCAL_AGENT_TOOLS", tools)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", lambda **_kwargs: model)

    class TestState(TypedDict):
        request: dict
        result: dict

    async def node(state):
        return {"result": await module.executor(state["request"])}

    workflow = StateGraph(TestState)
    workflow.add_node("execute", node)
    workflow.set_entry_point("execute")
    workflow.add_edge("execute", END)
    graph = workflow.compile()

    async def execute(state):
        return (await graph.ainvoke({"request": state}))["result"]

    return execute


class ToolModel:
    def bind_tools(self, _tools):
        return self


@pytest.mark.asyncio
async def test_executor_retains_original_service_mapping_when_plan_omits_parameters(monkeypatch):
    @tool
    async def read_logs(topic_id: str) -> str:
        """Read the configured log topic."""
        return topic_id

    class Model(ToolModel):
        async def ainvoke(self, messages):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if results:
                return AIMessage(content=results[-1].content)
            original_context = "\n".join(str(m.content) for m in messages)
            topic = "actual-topic-id" if "actual-topic-id" in original_context else "example-topic"
            return AIMessage(content="", tool_calls=[{"name": "read_logs", "args": {"topic_id": topic}, "id": "logs"}])

    executor = await setup_executor(monkeypatch, Model(), [read_logs])
    result = await executor({
        "input": 'Diagnose orders; CLS mapping: {"topic_id":"actual-topic-id"}',
        "plan": ["Read application logs"], "past_steps": [],
    })
    assert result["evidence"][0]["query"] == {"topic_id": "actual-topic-id"}


@pytest.mark.asyncio
async def test_executor_executes_dependent_tool_calls_and_keeps_all_evidence(monkeypatch):
    @tool
    async def find_service() -> str:
        """Find the service associated with an alert."""
        return "orders"

    @tool
    async def query_metric(service: str) -> str:
        """Read one service's CPU metric."""
        return f"{service}: CPU 95%"

    class Model(ToolModel):
        async def ainvoke(self, messages):
            results = [message for message in messages if isinstance(message, ToolMessage)]
            if not results:
                return AIMessage(
                    content="", tool_calls=[{"name": "find_service", "args": {}, "id": "find"}]
                )
            if len(results) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "query_metric",
                            "args": {"service": results[0].content},
                            "id": "metric",
                        }
                    ],
                )
            return AIMessage(content=results[-1].content)

    executor = await setup_executor(monkeypatch, Model(), [find_service, query_metric])
    result = await executor({"plan": ["Investigate alert", "Report"], "past_steps": []})
    assert result["past_steps"] == [("Investigate alert", "orders: CPU 95%")]
    assert [item["source"] for item in result["evidence"]] == ["find_service", "query_metric"]
    assert result["evidence"][1]["query"] == {"service": "orders"}
    assert result["plan"] == ["Report"]


@pytest.mark.asyncio
async def test_executor_can_use_previous_step_results_for_dependent_task(monkeypatch):
    class Model(ToolModel):
        async def ainvoke(self, messages):
            content = "\n".join(str(message.content) for message in messages)
            return AIMessage(
                content="orders confirmed"
                if "alert target: orders" in content
                else "missing target"
            )

    executor = await setup_executor(monkeypatch, Model(), [])
    result = await executor(
        {
            "input": "Diagnose orders",
            "plan": ["Analyze target"],
            "past_steps": [("Query alert", "alert target: orders")],
        }
    )
    assert result["past_steps"] == [("Analyze target", "orders confirmed")]


@pytest.mark.asyncio
async def test_executor_stops_unbounded_tool_requests_and_reports_failure(monkeypatch):
    @tool
    async def probe() -> str:
        """Read a metric."""
        return "still investigating"

    class Model(ToolModel):
        async def ainvoke(self, messages):
            return AIMessage(
                content="", tool_calls=[{"name": "probe", "args": {}, "id": str(len(messages))}]
            )

    executor = await setup_executor(monkeypatch, Model(), [probe])
    result = await executor({"plan": ["Investigate"], "past_steps": []})
    assert result.get("execution_errors")
    assert 1 <= len(result["evidence"]) <= 8
    assert "执行失败" in result["past_steps"][0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_text",
    [
        json.dumps({"success": False, "error": "timeout"}),
        [{"type": "text", "text": '{"success":false,"error":"timeout"}'}],
        json.dumps([{"type": "text", "text": '{"success":false,"error":"timeout"}'}]),
        "raised error",
    ],
)
async def test_executor_records_tool_failure_even_if_model_claims_success(monkeypatch, result_text):
    @tool
    async def probe() -> str:
        """Read a metric."""
        if result_text == "raised error":
            raise ValueError("monitor unavailable")
        return result_text

    class Model(ToolModel):
        async def ainvoke(self, messages):
            if any(isinstance(message, ToolMessage) for message in messages):
                return AIMessage(content="Everything is healthy")
            return AIMessage(content="", tool_calls=[{"name": "probe", "args": {}, "id": "probe"}])

    executor = await setup_executor(monkeypatch, Model(), [probe])
    result = await executor({"plan": ["Investigate"], "past_steps": []})
    assert result.get("execution_errors")
    assert result["evidence"][0]["success"] is False
