from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import config
from app.core.auth import ApiKeyMiddleware, validate_security_configuration
from app.core.security import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.models.alerts import AlertmanagerWebhook
from app.models.request import ChatRequest


def test_api_key_accepts_header_and_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "api_key", "a" * 64)
    monkeypatch.setattr(config, "alertmanager_webhook_token", "b" * 64)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/api/private")
    async def private():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/api/private").status_code == 401
    assert client.get("/api/private", headers={"X-API-Key": "a" * 64}).status_code == 200
    assert (
        client.get("/api/private", headers={"Authorization": f"Bearer {'a' * 64}"}).status_code
        == 200
    )


def test_webhook_fails_closed_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "alertmanager_webhook_token", "")
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.post("/api/alerts/webhook")
    async def webhook():
        return {"ok": True}

    assert TestClient(app).post("/api/alerts/webhook", json={}).status_code == 503


def test_security_configuration_rejects_public_unauthenticated_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "auth_enabled", False)
    monkeypatch.setattr(config, "web_login_enabled", False)
    monkeypatch.setattr(config, "host", "0.0.0.0")
    monkeypatch.setattr(config, "alertmanager_webhook_token", "b" * 64)
    with pytest.raises(RuntimeError, match="loopback"):
        validate_security_configuration()


def test_request_size_rate_limit_and_security_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "max_request_body_bytes", 4)
    monkeypatch.setattr(config, "rate_limit_enabled", True)
    monkeypatch.setattr(config, "rate_limit_expensive_per_minute", 1)
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.post("/api/chat")
    async def chat():
        return {"ok": True}

    client = TestClient(app)
    first = client.post("/api/chat", content=b"1234")
    second = client.post("/api/chat", content=b"1234")
    oversized = client.post("/api/other", content=b"12345")

    assert first.status_code == 200
    assert second.status_code == 429
    assert oversized.status_code == 413
    assert first.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in first.headers["content-security-policy"]


def test_request_models_bound_untrusted_input() -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"Id": "bad/session", "Question": "hello"})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"Id": "session", "Question": "x" * 10_001})
    with pytest.raises(ValidationError):
        AlertmanagerWebhook.model_validate(
            {
                "alerts": [
                    {"status": "firing", "labels": {"alertname": str(index)}}
                    for index in range(101)
                ]
            }
        )


def test_frontend_sanitizes_markdown_before_inner_html() -> None:
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert "DOMPurify.sanitize(html" in app_js
    assert "dompurify@3.4.16" in index_html
    assert 'integrity="sha384-' in index_html


def test_frontend_uses_account_session_instead_of_api_key_input() -> None:
    app_js = Path("static/app.js").read_text(encoding="utf-8")
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    login_html = Path("static/login.html").read_text(encoding="utf-8")
    login_js = Path("static/login.js").read_text(encoding="utf-8")

    assert "apiKeyInput" not in index_html
    assert "aiOnCallApiKey" not in app_js
    assert "/api/auth/me" in app_js
    assert "X-CSRF-Token" in app_js
    assert 'autocomplete="current-password"' in login_html
    assert "sessionStorage.removeItem('aiOnCallApiKey')" in login_js
