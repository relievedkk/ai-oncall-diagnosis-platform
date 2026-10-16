"""Functional API regressions with actual routes and external providers replaced."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import chat, health, remediation
from app.config import config
from app.core import milvus_client
from app.core.security import RequestSizeLimitMiddleware


def chat_client(user_id=1):
    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.auth_kind = "session"
        request.state.principal = SimpleNamespace(user_id=user_id, role="operator")
        return await call_next(request)

    app.include_router(chat.router, prefix="/api")
    return TestClient(app)


def test_chat_provider_failure_is_an_http_error(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("private-provider-detail")

    monkeypatch.setattr(chat.rag_agent_service, "query", unavailable)
    response = chat_client().post("/api/chat", json={"Id": "test", "Question": "hello"})
    assert response.status_code == 503
    assert response.json()["data"]["success"] is False
    assert "private-provider-detail" not in response.text


def test_chat_session_history_and_clear_are_scoped_to_authenticated_user(monkeypatch):
    histories = {}

    async def answer(question, session_id):
        histories.setdefault(session_id, []).append({"role": "user", "content": question})
        return "ok"

    async def history(session_id):
        return histories.get(session_id, [])

    async def clear(session_id):
        histories.pop(session_id, None)
        return True

    monkeypatch.setattr(chat.rag_agent_service, "query", answer)
    monkeypatch.setattr(chat.rag_agent_service, "get_session_history", history)
    monkeypatch.setattr(chat.rag_agent_service, "clear_session", clear)
    first, second = chat_client(1), chat_client(2)
    assert first.post("/api/chat", json={"Id": "same", "Question": "user-one-secret"}).status_code == 200
    assert second.get("/api/chat/session/same").json()["message_count"] == 0
    assert second.post("/api/chat/clear", json={"session_id": "same"}).status_code == 200
    assert first.get("/api/chat/session/same").json()["message_count"] == 1


@pytest.mark.parametrize("role", ["viewer", "unknown"])
def test_read_only_or_unknown_role_cannot_approve_remediation(role, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.auth_kind = "session"
        request.state.principal = SimpleNamespace(user_id=1, role=role)
        return await call_next(request)

    async def must_not_decide(*args, **kwargs):
        raise AssertionError("unauthorized decision reached service")

    monkeypatch.setattr(remediation.remediation_service, "decide", must_not_decide)
    app.include_router(remediation.router, prefix="/api")
    response = TestClient(app).post("/api/remediations/action/decision", json={"decision": "approve"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_ready_requires_diagnosis_consumers(monkeypatch):
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    async def database_ok():
        return True

    async def queues_without_workers():
        return {config.celery_default_queue: {"consumers": 0}, config.celery_critical_queue: {"consumers": 1}}

    monkeypatch.setattr(health.database_manager, "health_check", database_ok)
    monkeypatch.setattr(health.queue_observer, "rabbitmq_queues", queues_without_workers)
    assert (await health.readiness()).status_code == 503


def test_milvus_health_probes_the_server_instead_of_registered_aliases():
    class DisconnectedClient:
        def list_collections(self, **kwargs):
            raise ConnectionError("server is unavailable")

    manager = milvus_client.MilvusClientManager()
    manager._client = DisconnectedClient()
    assert manager.health_check() is False


def test_chunked_body_cannot_bypass_global_size_limit(monkeypatch):
    monkeypatch.setattr(config, "max_request_body_bytes", 4)
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    client = TestClient(app)
    assert client.post("/echo", content=iter([b"12", b"34"])).status_code == 200
    assert client.post("/echo", content=iter([b"12", b"345"])).status_code == 413
