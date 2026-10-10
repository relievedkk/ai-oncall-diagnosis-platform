from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.config import config
from app.core.auth import ApiKeyMiddleware
from app.services.auth_service import AuthService, CreatedSession, SessionPrincipal, auth_service


def _principal(csrf_token: str = "csrf-value") -> SessionPrincipal:
    return SessionPrincipal(
        user_id=1,
        username="admin",
        display_name="平台管理员",
        role="admin",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        csrf_hash=AuthService.hash_secret(csrf_token),
    )


def test_password_hash_uses_argon2id() -> None:
    service = AuthService()
    password_hash = service.password_hash.hash("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert service.password_hash.verify("correct horse battery staple", password_hash)


def test_cookie_session_requires_csrf_for_mutating_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "web_login_enabled", True)
    monkeypatch.setattr(config, "api_key", "a" * 64)

    async def fake_principal(_token: str) -> SessionPrincipal:
        return _principal()

    monkeypatch.setattr(auth_service, "get_principal", fake_principal)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/api/private")
    async def read_private():
        return {"ok": True}

    @app.post("/api/private")
    async def write_private():
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set(config.session_cookie_name, "session-value")
    client.cookies.set(config.csrf_cookie_name, "csrf-value")

    assert client.get("/api/private").status_code == 200
    assert client.post("/api/private").status_code == 403
    assert client.post("/api/private", headers={"X-CSRF-Token": "csrf-value"}).status_code == 200


def test_login_sets_httponly_session_and_readable_csrf_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "web_login_enabled", True)
    monkeypatch.setattr(config, "session_cookie_secure", False)

    async def fake_login(_username: str, _password: str, _remember: bool) -> CreatedSession:
        return CreatedSession(
            token="session-value",
            csrf_token="csrf-value",
            principal=_principal(),
        )

    monkeypatch.setattr(auth_service, "login", fake_login)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    response = TestClient(app).post(
        "/api/auth/login",
        json={"username": "admin", "password": "strong-password", "remember": False},
    )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if item.startswith("oncall_session="))
    csrf_cookie = next(item for item in cookies if item.startswith("oncall_csrf="))
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "HttpOnly" not in csrf_cookie


def test_logout_revokes_session_and_clears_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    revoked: list[str] = []

    async def fake_csrf_is_valid(_token: str, csrf_token: str) -> bool:
        return csrf_token == "csrf-value"

    async def fake_logout(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(auth_service, "csrf_is_valid", fake_csrf_is_valid)
    monkeypatch.setattr(auth_service, "logout", fake_logout)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    client = TestClient(app)
    client.cookies.set(config.session_cookie_name, "session-value")
    client.cookies.set(config.csrf_cookie_name, "csrf-value")

    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": "csrf-value"})

    assert response.status_code == 204
    assert revoked == ["session-value"]
    cleared = response.headers.get_list("set-cookie")
    assert any(
        item.startswith(f"{config.session_cookie_name}=") and "Max-Age=0" in item
        for item in cleared
    )
    assert any(
        item.startswith(f"{config.csrf_cookie_name}=") and "Max-Age=0" in item for item in cleared
    )
