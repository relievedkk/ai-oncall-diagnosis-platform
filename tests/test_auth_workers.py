"""Password verification must not monopolize the API event loop or CPU queue."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router
from app.config import config
from app.services import auth_service as auth_module


class MissingUserSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def scalar(self, query):
        return None


@pytest.fixture
def missing_user(monkeypatch):
    monkeypatch.setattr(auth_module.database_manager, "session_factory", MissingUserSession)


async def test_login_password_work_leaves_event_loop_responsive(missing_user):
    service = auth_module.AuthService()
    ticked = False
    observed = []

    def verify(*args):
        time.sleep(0.1)
        observed.append(ticked)
        return False

    service.password_hash = SimpleNamespace(verify=verify)

    async def tick():
        nonlocal ticked
        await asyncio.sleep(0.01)
        ticked = True

    ticker = asyncio.create_task(tick())
    try:
        with pytest.raises(auth_module.AuthenticationError):
            await service.login("missing", "password", False)
        await ticker
        assert observed == [True]
    finally:
        if hasattr(service, "shutdown"):
            await service.shutdown()


async def test_cancelled_login_keeps_capacity_until_worker_finishes(missing_user):
    service = auth_module.AuthService(password_workers=1, password_queue_size=0)
    started = threading.Event()
    release = threading.Event()

    def verify(*args):
        started.set()
        release.wait(2)
        return False

    service.password_hash = SimpleNamespace(verify=verify)
    request = asyncio.create_task(service.login("missing", "password", False))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(auth_module.AuthenticationBusyError):
            await service.login("missing", "password", False)
        shutdown = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0.01)
        assert not shutdown.done()
        release.set()
        await shutdown
        # A later lifespan may reuse the singleton after a complete shutdown.
        with pytest.raises(auth_module.AuthenticationError):
            await service.login("missing", "password", False)
    finally:
        release.set()
        await service.shutdown()


async def test_password_pool_bounds_active_and_queued_work(missing_user):
    service = auth_module.AuthService(password_workers=1, password_queue_size=1)
    release = threading.Event()
    started = threading.Event()
    active = 0
    peak = 0

    def verify(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        release.wait(2)
        active -= 1
        return False

    service.password_hash = SimpleNamespace(verify=verify)
    first = asyncio.create_task(service.login("missing", "password", False))
    queued = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(service.login("missing", "password", False))
        await asyncio.sleep(0.01)
        with pytest.raises(auth_module.AuthenticationBusyError):
            await service.login("missing", "password", False)
        assert peak == 1
    finally:
        release.set()
        await asyncio.gather(*(item for item in (first, queued) if item), return_exceptions=True)
        await service.shutdown()


def test_busy_login_returns_retryable_service_error(monkeypatch):
    monkeypatch.setattr(config, "web_login_enabled", True)

    async def busy(*args):
        raise auth_module.AuthenticationBusyError("internal worker details")

    monkeypatch.setattr(auth_module.auth_service, "login", busy)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/auth/login", json={"username": "user", "password": "password"}
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "internal worker details" not in response.text
