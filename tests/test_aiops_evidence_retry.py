import importlib
import json
from typing import TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph


async def execute_in_graph(module, request):
    class State(TypedDict):
        request: dict
        result: dict

    async def node(state):
        return {"result": await module.executor(state["request"])}

    workflow = StateGraph(State)
    workflow.add_node("execute", node)
    workflow.set_entry_point("execute")
    workflow.add_edge("execute", END)
    return (await workflow.compile().ainvoke({"request": request}))["result"]


@pytest.mark.asyncio
async def test_report_model_receives_raw_evidence_not_only_step_summary():
    module = importlib.import_module("app.agent.aiops.replanner")
    received = []

    async def respond(value):
        received.extend(value.to_messages())
        return module.Response(response="report")

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)

    await module._generate_response(
        {
            "input": "diagnose orders",
            "past_steps": [("query metric", "CPU high; summary omitted the evidence ID")],
            "evidence": [
                {
                    "source": "query_service_metrics",
                    "success": True,
                    "query": {"service_name": "orders", "metric": "cpu"},
                    "result": json.dumps({"evidence_id": "OBS-RAW-731", "value": 96}),
                }
            ],
        },
        Model(),
    )
    assert any("OBS-RAW-731" in str(m.content) for m in received)
    assert any("不可信" in str(m.content) for m in received)


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable,expected_calls,failed", [(True, 2, False), (False, 1, True)])
async def test_read_only_transient_retry_retains_attempts_without_stale_failure(
    monkeypatch, retryable, expected_calls, failed
):
    module = importlib.import_module("app.agent.aiops.executor")
    calls = []

    @tool
    async def query_service_metrics(service_name: str, metric: str) -> dict:
        """Read current service metric."""
        calls.append((service_name, metric))
        if len(calls) == 1:
            return {"success": False, "retryable": retryable, "error": "upstream timeout"}
        return {"success": True, "value": 96, "evidence_id": "OBS-RECOVERED"}

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            if isinstance(messages[-1], HumanMessage):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "query_service_metrics",
                            "args": {"service_name": "orders", "metric": "cpu"},
                            "id": "metric-call",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="observations reported")

    async def no_client():
        return None

    async def no_tools(client):
        return [], None

    monkeypatch.setattr(module, "DEFAULT_LOCAL_AGENT_TOOLS", [query_service_metrics])
    monkeypatch.setattr(module, "get_mcp_client_with_retry", no_client)
    monkeypatch.setattr(module, "load_mcp_tools_safe", no_tools)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", lambda **kwargs: Model())
    result = await execute_in_graph(module, {"input": "diagnose orders", "plan": ["query metric"]})
    assert len(calls) == expected_calls
    assert len(result["evidence"]) == expected_calls
    assert bool(result["execution_errors"]) is failed
    if retryable:
        assert result["evidence"][0]["success"] is False
        assert result["evidence"][1]["success"] is True


@pytest.mark.asyncio
async def test_write_tool_is_not_automatically_retried(monkeypatch):
    module = importlib.import_module("app.agent.aiops.executor")
    calls = []

    @tool
    async def restart_instance(instance_id: str) -> dict:
        """Restart an instance."""
        calls.append(instance_id)
        return {"success": False, "retryable": True, "error": "uncertain write outcome"}

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            if isinstance(messages[-1], HumanMessage):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "restart_instance",
                            "args": {"instance_id": "i-1"},
                            "id": "write-call",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="write failed")

    async def no_client():
        return None

    async def no_tools(client):
        return [], None

    monkeypatch.setattr(module, "DEFAULT_LOCAL_AGENT_TOOLS", [restart_instance])
    monkeypatch.setattr(module, "get_mcp_client_with_retry", no_client)
    monkeypatch.setattr(module, "load_mcp_tools_safe", no_tools)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", lambda **kwargs: Model())
    result = await execute_in_graph(module, {"input": "task", "plan": ["task"]})
    assert calls == ["i-1"]
    assert result["execution_errors"]


@pytest.mark.asyncio
async def test_retryable_read_failure_is_still_failed_after_two_attempts(monkeypatch):
    module = importlib.import_module("app.agent.aiops.executor")
    calls = []

    @tool
    async def query_service_logs(service_name: str) -> dict:
        """Read application logs."""
        calls.append(service_name)
        return {"success": False, "retryable": True, "error": "timeout"}

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            if isinstance(messages[-1], HumanMessage):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "query_service_logs",
                            "args": {"service_name": "orders"},
                            "id": "logs-call",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="logs unavailable")

    async def no_client():
        return None

    async def no_tools(client):
        return [], None

    monkeypatch.setattr(module, "DEFAULT_LOCAL_AGENT_TOOLS", [query_service_logs])
    monkeypatch.setattr(module, "get_mcp_client_with_retry", no_client)
    monkeypatch.setattr(module, "load_mcp_tools_safe", no_tools)
    monkeypatch.setattr(module.llm_factory, "create_chat_model", lambda **kwargs: Model())
    result = await execute_in_graph(module, {"input": "task", "plan": ["task"]})
    assert calls == ["orders", "orders"]
    assert len(result["evidence"]) == 2
    assert result["execution_errors"]
