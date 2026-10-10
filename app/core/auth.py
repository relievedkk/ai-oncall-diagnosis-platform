"""API Key authentication and filesystem authorization helpers."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import config
from app.services.auth_service import auth_service

_PUBLIC_PATHS = {
    "/login",
    "/live",
    "/api/auth/login",
    "/api/auth/logout",
}

_PLACEHOLDER_VALUES = {
    "replace-with-random-64-character-api-key",
    "replace-with-random-64-character-token",
    "replace-with-random-admin-password",
}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Accept machine API keys or revocable browser sessions."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/api/alerts/webhook":
            if not _is_strong_secret(config.alertmanager_webhook_token):
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Alertmanager webhook authentication is not configured"},
                )
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {config.alertmanager_webhook_token}"
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401, content={"detail": "Invalid Alertmanager webhook token"}
                )
            return await call_next(request)

        if (
            not config.auth_enabled
            or request.method == "OPTIONS"
            or path in _PUBLIC_PATHS
            or path.startswith("/static/")
        ):
            return await call_next(request)

        if not config.api_key:
            return JSONResponse(
                status_code=503,
                content={"detail": "API authentication is enabled but API_KEY is not configured"},
            )

        supplied_key = request.headers.get("X-API-Key", "")
        if not supplied_key:
            authorization = request.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                supplied_key = authorization.removeprefix("Bearer ")

        if supplied_key:
            if not secrets.compare_digest(supplied_key, config.api_key):
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
            request.state.auth_kind = "api_key"
            return await call_next(request)

        if config.web_login_enabled:
            session_token = request.cookies.get(config.session_cookie_name, "")
            principal = await auth_service.get_principal(session_token)
            if principal is not None:
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    csrf_cookie = request.cookies.get(config.csrf_cookie_name, "")
                    csrf_header = request.headers.get("X-CSRF-Token", "")
                    valid_csrf = bool(
                        csrf_cookie
                        and csrf_header
                        and secrets.compare_digest(csrf_cookie, csrf_header)
                        and secrets.compare_digest(
                            principal.csrf_hash,
                            auth_service.hash_secret(csrf_header),
                        )
                    )
                    if not valid_csrf:
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "Invalid CSRF token"},
                        )
                request.state.auth_kind = "session"
                request.state.principal = principal
                return await call_next(request)

        if path == "/" or "text/html" in request.headers.get("Accept", ""):
            return RedirectResponse(url="/login", status_code=303)

        return JSONResponse(status_code=401, content={"detail": "Authentication required"})


def is_path_allowed(target: str | Path) -> bool:
    """Return whether *target* is within a configured indexing root."""
    try:
        candidate = Path(target).resolve(strict=False)
        for root in config.allowed_index_root_list:
            allowed_root = Path(root).resolve(strict=False)
            try:
                candidate.relative_to(allowed_root)
                return True
            except ValueError:
                continue
    except (OSError, ValueError):
        return False
    return False


def _is_strong_secret(value: str) -> bool:
    """Return whether a configured secret is suitable for authentication."""
    return len(value) >= 32 and value not in _PLACEHOLDER_VALUES


def validate_security_configuration() -> None:
    """Fail closed when the process would otherwise start in an unsafe state."""
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if not config.auth_enabled and config.host not in loopback_hosts:
        raise RuntimeError("AUTH_ENABLED=false is only allowed when HOST is a loopback address")
    if config.auth_enabled and not _is_strong_secret(config.api_key):
        raise RuntimeError("AUTH_ENABLED=true requires an API_KEY of at least 32 characters")
    if config.web_login_enabled:
        if len(config.admin_username.strip()) < 3:
            raise RuntimeError("WEB_LOGIN_ENABLED=true requires ADMIN_USERNAME")
        weak_password = (
            len(config.admin_password) < 12 or config.admin_password in _PLACEHOLDER_VALUES
        )
        if weak_password and not config.allow_weak_admin_password:
            raise RuntimeError(
                "WEB_LOGIN_ENABLED=true requires an ADMIN_PASSWORD of at least 12 characters"
            )
    if not _is_strong_secret(config.alertmanager_webhook_token):
        raise RuntimeError(
            "ALERTMANAGER_WEBHOOK_TOKEN must be configured with at least 32 characters"
        )
