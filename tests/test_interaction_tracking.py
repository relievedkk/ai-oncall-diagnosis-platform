import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_chat_returns_owned_interaction_and_records_completion(monkeypatch):
    from app.api import chat
    from app.services import usage_feedback

    captured = []

    async def start(user, session, origin):
        captured.append((user, origin))
        return "interaction-test"

    async def finish(identity, status, **values):
        captured.append((identity, status, values))

    async def answer(*args, **kwargs):
        return "answer"

    monkeypatch.setattr(
        usage_feedback, "usage_feedback_service", SimpleNamespace(start=start, finish=finish)
    )
    monkeypatch.setattr(chat.rag_agent_service, "query", answer)
    app = FastAPI()
    app.state.usage_tracking_started = True

    @app.middleware("http")
    async def owner(request: Request, call_next):
        request.state.principal = SimpleNamespace(user_id=7)
        request.state.auth_kind = "session"
        return await call_next(request)

    app.include_router(chat.router, prefix="/api")
    response = TestClient(app).post("/api/chat", json={"Id": "conversation", "Question": "hello"})
    assert response.json()["data"]["interaction_id"] == "interaction-test"
    assert captured[0][0] == 7
    assert captured[-1][1] == "completed"


@pytest.mark.asyncio
async def test_stream_completion_waits_for_feedback_record_commit(monkeypatch):
    from app.api import chat
    from app.models.request import ChatRequest
    from app.services import usage_feedback

    finished = []

    async def start(*args):
        return "stream-id"

    async def finish(*args, **kwargs):
        finished.append(True)

    async def stream(*args, **kwargs):
        yield {"type": "content", "data": "answer"}
        yield {"type": "complete"}

    monkeypatch.setattr(
        usage_feedback, "usage_feedback_service", SimpleNamespace(start=start, finish=finish)
    )
    monkeypatch.setattr(chat.rag_agent_service, "query_stream", stream)
    app = FastAPI()
    app.state.usage_tracking_started = True
    request = Request({"type": "http", "app": app})
    response = await chat.chat_stream(ChatRequest(Id="stream", Question="hello"), request)
    async for event in response.body_iterator:
        if json.loads(event["data"]).get("type") == "done":
            assert finished


@pytest.mark.asyncio
async def test_closing_stream_records_cancellation_and_resets_capture(monkeypatch):
    from app.api import chat
    from app.core.model_protection import _capture
    from app.models.request import ChatRequest
    from app.services import usage_feedback

    finished = []

    async def start(*args):
        return "cancel-id"

    async def finish(identity, status, **kwargs):
        finished.append(status)

    async def stream(*args, **kwargs):
        yield {"type": "content", "data": "partial"}
        yield {"type": "complete"}

    monkeypatch.setattr(
        usage_feedback, "usage_feedback_service", SimpleNamespace(start=start, finish=finish)
    )
    monkeypatch.setattr(chat.rag_agent_service, "query_stream", stream)
    app = FastAPI()
    app.state.usage_tracking_started = True
    request = Request({"type": "http", "app": app})
    response = await chat.chat_stream(ChatRequest(Id="cancel", Question="hello"), request)
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert finished == ["cancelled"]
    assert _capture.get() is None
