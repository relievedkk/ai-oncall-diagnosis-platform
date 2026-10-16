"""Regression coverage for isolated, checkpointed diagnostic executions."""

import importlib
import json
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver

from app.services.aiops_service import AIOpsService


def make_service(monkeypatch, *, fail_step=False, propose_remediation=False):
    module = importlib.import_module("app.services.aiops_service")

    async def plan(state):
        return {"plan": [state["input"]]}

    async def execute(state):
        task = state["plan"][0]
        result = {
            "plan": [],
            "past_steps": [(task, f"result:{task}")],
            "evidence": [{"source": task, "result": task}],
        }
        if fail_step:
            result["execution_errors"] = ["monitor unavailable"]
        return result

    async def replan(state):
        result = {"response": ",".join(step for step, _ in state["past_steps"])}
        if propose_remediation:
            result["remediation"] = {
                "action_type": "restart_instance",
                "service_name": "orders",
                "instance_id": "orders-1",
                "reason": "Unhealthy instance",
            }
        return result

    monkeypatch.setattr(module, "planner", plan)
    monkeypatch.setattr(module, "executor", execute)
    monkeypatch.setattr(module, "replanner", replan)
    service = AIOpsService()
    service.checkpointer = MemorySaver()
    service.graph = service._build_graph(service.checkpointer)
    return service


@pytest.mark.asyncio
async def test_new_execution_in_same_session_does_not_reuse_previous_evidence(monkeypatch):
    service = make_service(monkeypatch)
    first = [event async for event in service.execute("old diagnosis", "shared")]
    second = [event async for event in service.execute("new diagnosis", "shared")]

    assert first[-1]["response"] == "old diagnosis"
    assert second[-1]["response"] == "new diagnosis"
    snapshot = await service.graph.aget_state({"configurable": {"thread_id": "shared"}})
    assert [tuple(step) for step in snapshot.values["past_steps"]] == [
        ("new diagnosis", "result:new diagnosis")
    ]
    assert snapshot.values["evidence"] == [{"source": "new diagnosis", "result": "new diagnosis"}]


@pytest.mark.asyncio
async def test_explicit_resume_continues_pending_checkpoint_without_replanning(monkeypatch):
    service = make_service(monkeypatch)
    await service.graph.aupdate_state(
        {"configurable": {"thread_id": "pending"}},
        {
            "input": "original",
            "plan": ["pending step"],
            "past_steps": [],
            "evidence": [],
            "response": "",
        },
        as_node="planner",
    )
    events = [event async for event in service.execute("ignored", "pending", resume=True)]
    assert events[-1]["response"] == "pending step"
    assert all(event["type"] != "plan" for event in events)


@pytest.mark.asyncio
async def test_resume_without_checkpoint_starts_fresh(monkeypatch):
    service = make_service(monkeypatch)
    events = [event async for event in service.execute("task", "missing", resume=True)]
    assert events[-1]["type"] == "complete"
    assert events[-1]["response"] == "task"


@pytest.mark.asyncio
async def test_execution_with_failed_step_does_not_emit_successful_completion(monkeypatch):
    service = make_service(monkeypatch, fail_step=True)
    events = [event async for event in service.execute("task", "failed")]
    assert events[-1]["type"] == "error"
    assert all(event["type"] != "complete" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_failed_execution_never_exposes_remediation_proposal(monkeypatch, resume):
    service = make_service(monkeypatch, fail_step=True, propose_remediation=True)
    if resume:
        await service.graph.aupdate_state(
            {"configurable": {"thread_id": "failed"}},
            {
                "input": "task",
                "plan": [],
                "past_steps": [("query", "failed")],
                "evidence": [],
                "response": "",
                "execution_errors": ["monitor unavailable"],
            },
            as_node="executor",
        )
    events = [event async for event in service.execute("task", "failed", resume=resume)]
    assert events[-1]["type"] == "error"
    assert all(event["type"] != "remediation_proposed" for event in events)
    assert all("remediation" not in event for event in events)


@pytest.mark.parametrize(
    "latest",
    [
        {"success": True, "alerts": [{"alertname": "CPUHigh"}], "total": 1},
        {"success": False, "error": "timeout"},
        "invalid JSON",
    ],
)
def test_newer_alert_scan_invalidates_older_empty_result(latest):
    from app.agent.aiops.replanner import _find_empty_alert_scan

    evidence = [
        {
            "source": "query_prometheus_alerts",
            "result": json.dumps({"success": True, "alerts": [], "total": 0}),
        },
        {"source": "query_prometheus_alerts", "result": latest},
    ]
    assert _find_empty_alert_scan(evidence) is None


@pytest.mark.asyncio
async def test_planner_model_failure_is_recorded_in_workflow_state(monkeypatch):
    module = importlib.import_module("app.agent.aiops.planner")

    async def no_client():
        return None

    async def no_tools(_client):
        return [], None

    async def no_knowledge(_query):
        return ""

    async def unavailable(_messages):
        raise RuntimeError("model unavailable")

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return RunnableLambda(unavailable)

    monkeypatch.setattr(module, "get_mcp_client_with_retry", no_client)
    monkeypatch.setattr(module, "load_mcp_tools_safe", no_tools)
    monkeypatch.setattr(module, "retrieve_knowledge", SimpleNamespace(ainvoke=no_knowledge))
    monkeypatch.setattr(module.llm_factory, "create_chat_model", lambda **_kwargs: Model())
    result = await module.planner({"input": "diagnose"})
    assert result.get("execution_errors")
    assert "model unavailable" in result["execution_errors"][0]


@pytest.mark.asyncio
async def test_report_generation_failure_is_recorded_with_partial_evidence():
    module = importlib.import_module("app.agent.aiops.replanner")

    async def unavailable(_messages):
        raise RuntimeError("model unavailable")

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return RunnableLambda(unavailable)

    result = await module._generate_response(
        {"input": "diagnose", "past_steps": [("query", "orders CPU: 95%")]}, Model()
    )
    assert "orders CPU: 95%" in result["response"]
    assert result.get("execution_errors")
