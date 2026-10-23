import asyncio

import pytest
from fastapi import FastAPI

from app.config import config
from app.core.security import RequestSizeLimitMiddleware


@pytest.mark.asyncio
async def test_body_reading_has_a_total_deadline(monkeypatch):
    monkeypatch.setitem(config.__dict__, "request_body_timeout_seconds", 0.03)
    reached = False

    async def downstream(scope, receive, send):
        nonlocal reached
        reached = True

    async def stalled_receive():
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "headers": [(b"content-length", b"2")],
    }
    await asyncio.wait_for(
        RequestSizeLimitMiddleware(downstream)(scope, stalled_receive, send), 0.5
    )
    assert sent[0]["status"] == 408
    assert not reached


@pytest.mark.asyncio
async def test_unauthenticated_body_is_not_read_or_admitted(monkeypatch):
    from app.main import app

    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "web_login_enabled", False)
    monkeypatch.setattr(config, "api_key", "a" * 64)
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "http_version": "1.1",
        "server": ("localhost", 9900),
        "client": ("127.0.0.22", 50000),
        "headers": [(b"host", b"localhost"), (b"content-length", b"100")],
    }
    await asyncio.wait_for(app(scope, receive, send), 0.5)
    assert sent[0]["status"] == 401
    assert reads == 0


@pytest.mark.asyncio
async def test_slow_authenticated_body_does_not_hold_model_permit(monkeypatch):
    import httpx

    from app.core.auth import ApiKeyMiddleware
    from app.core.model_protection import ModelAdmissionMiddleware

    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "web_login_enabled", False)
    monkeypatch.setattr(config, "api_key", "a" * 64)
    monkeypatch.setitem(config.__dict__, "request_body_timeout_seconds", 0.05)
    monkeypatch.setattr(config, "chat_max_concurrent", 1)
    monkeypatch.setattr(config, "chat_max_waiting", 0)
    app = FastAPI()
    # Match the production ordering.
    from app.main import app as production

    wanted = {ApiKeyMiddleware, RequestSizeLimitMiddleware, ModelAdmissionMiddleware}
    for middleware in reversed([m for m in production.user_middleware if m.cls in wanted]):
        app.add_middleware(middleware.cls)

    @app.post("/api/chat")
    async def chat():
        return {"ok": True}

    stalled = asyncio.Event()

    async def slow_body():
        yield b"{"
        stalled.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        first = asyncio.create_task(
            client.post("/api/chat", headers={"X-API-Key": "a" * 64}, content=slow_body())
        )
        await stalled.wait()
        try:
            response = await client.post("/api/chat", headers={"X-API-Key": "a" * 64}, json={})
            assert response.status_code == 200
            assert (await first).status_code == 408
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
