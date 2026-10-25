"""Provider faults through real RAG service and HTTP/SSE API boundaries."""

import importlib
import json

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI


@pytest.fixture
def quota_pipeline(monkeypatch):
    from app.config import config
    from app.core.model_protection import provider_circuit
    from app.services.shared_model_quota import SharedQuotaExceeded, SharedQuotaUnavailable

    monkeypatch.setattr(config, "dashscope_api_key", "offline-test-placeholder")
    monkeypatch.setattr(config, "usage_tracking_enabled", False)
    monkeypatch.setattr(config, "shared_model_quota_enabled", False)
    monkeypatch.setattr(provider_circuit, "open_until", 0)
    module = importlib.import_module("app.api.chat")
    from app.services.rag_agent_service import RagAgentService

    service = RagAgentService(streaming=False)
    service._agent_initialized = True
    failures = {
        "quota": (SharedQuotaExceeded(17), 429, "shared_model_quota_exceeded", 17),
        "unavailable": (SharedQuotaUnavailable(), 503, "shared_model_quota_unavailable", 2),
        "generic": (RuntimeError("SYNTHETIC_PROVIDER_SECRET"), 503, None, None),
    }
    for error, *_ in failures.values():
        # The public boundary must never derive its message from exception text.
        error.args = ("SYNTHETIC_PROVIDER_SECRET",)
    selected = [failures["quota"][0]]

    async def fail_provider(*args, **kwargs):
        raise selected[0]

    async def fail_provider_stream(*args, **kwargs):
        yield await fail_provider()

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fail_provider)
    monkeypatch.setattr(ChatOpenAI, "_astream", fail_provider_stream)

    class Agent:
        async def ainvoke(self, input, config):
            message = await service.model.ainvoke(input["messages"])
            return {"messages": [message]}

        async def astream(self, input, config, stream_mode):
            # The response has already emitted content before the provider fails.
            yield AIMessageChunk(content="partial answer"), {"langgraph_node": "model"}
            async for message in service.model.astream(input["messages"]):
                yield message, {"langgraph_node": "model"}

    service.agent = Agent()
    monkeypatch.setattr(module, "rag_agent_service", service)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    return app, service, failures, selected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quota", "unavailable", "generic"])
async def test_nonstream_quota_response_status_retry_and_legacy_shape(quota_pipeline, kind):
    app, _service, failures, selected = quota_pipeline
    error, status, error_code, retry_after = failures[kind]
    selected[0] = error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat", json={"Id": "quota-api", "Question": "diagnose latency"})
    assert response.status_code == status
    body = response.json()
    assert body["code"] == status and body["message"] == "error"
    assert body["data"]["success"] is False and body["data"]["answer"] is None
    assert isinstance(body["data"]["errorMessage"], str)
    assert "SYNTHETIC_PROVIDER_SECRET" not in response.text
    if error_code:
        assert response.headers["Retry-After"] == str(retry_after)
        assert body["data"]["error_code"] == error_code
        assert body["data"]["retry_after"] == retry_after
    else:
        assert "error_code" not in body["data"]
        assert body["data"]["errorMessage"] == "对话服务暂时不可用"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quota", "unavailable", "generic"])
async def test_actual_rag_stream_preserves_safe_quota_fields(quota_pipeline, kind):
    _app, service, failures, selected = quota_pipeline
    error, _status, error_code, retry_after = failures[kind]
    selected[0] = error
    events = [chunk async for chunk in service.query_stream("diagnose latency", "quota-stream")]
    assert events[0]["type"] == "content"
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "complete" for event in events)
    assert "SYNTHETIC_PROVIDER_SECRET" not in json.dumps(events)
    if error_code:
        assert events[-1]["error_code"] == error_code
        assert events[-1]["retry_after"] == retry_after
    else:
        assert events[-1] == {"type": "error", "data": "对话服务暂时不可用"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quota", "unavailable", "generic"])
async def test_sse_keeps_started_http_status_and_typed_quota_metadata(quota_pipeline, kind):
    app, _service, failures, selected = quota_pipeline
    error, _status, error_code, retry_after = failures[kind]
    selected[0] = error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat_stream", json={"Id": "quota-api", "Question": "diagnose latency"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: ")]
    assert events[0] == {"type": "content", "data": "partial answer"}
    assert events[-1]["type"] == "error" and isinstance(events[-1]["data"], str)
    assert not any(event["type"] == "done" for event in events)
    assert "SYNTHETIC_PROVIDER_SECRET" not in response.text
    if error_code:
        assert events[-1]["error_code"] == error_code
        assert events[-1]["retry_after"] == retry_after
    else:
        assert events[-1] == {"type": "error", "data": "对话服务暂时不可用"}
