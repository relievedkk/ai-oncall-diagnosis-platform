import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import config
from app.core.security import RateLimitMiddleware


@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
def test_rotating_unverified_headers_cannot_reset_login_limit(monkeypatch, header):
    monkeypatch.setattr(config, "rate_limit_enabled", True)
    monkeypatch.setattr(config, "rate_limit_login_per_minute", 5)
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/api/auth/login", status_code=401)
    async def login():
        return {"detail": "invalid login"}

    with TestClient(app) as client:
        statuses = [
            client.post(
                "/api/auth/login",
                headers={
                    header: ("Bearer " if header == "Authorization" else "") + f"invalid-{i}",
                },
            ).status_code
            for i in range(6)
        ]
    assert statuses == [401, 401, 401, 401, 401, 429]
