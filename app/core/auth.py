"""API Key authentication and filesystem authorization helpers."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import config


_PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc", "/metrics"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Protect API routes with the configured ``X-API-Key`` header."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
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
        if not secrets.compare_digest(supplied_key, config.api_key):
            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})

        return await call_next(request)


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
