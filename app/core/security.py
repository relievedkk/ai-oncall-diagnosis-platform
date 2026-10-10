"""HTTP hardening middleware for the public API surface."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import config


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add browser security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'self'",
                    "script-src 'self' https://cdn.jsdelivr.net",
                    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
                    "img-src 'self' data:",
                    "connect-src 'self'",
                    "font-src 'self' https://cdn.jsdelivr.net",
                    "object-src 'none'",
                    "base-uri 'self'",
                    "frame-ancestors 'none'",
                    "form-action 'self'",
                )
            ),
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject declared oversized requests before multipart parsing begins."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.max_request_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body is too large"},
                    )
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small in-process limiter; production gateways should enforce a shared limit too."""

    _EXPENSIVE_PATHS = {"/api/chat", "/api/chat_stream", "/api/aiops"}
    _UPLOAD_PATHS = {"/api/upload", "/api/index_directory"}
    _LOGIN_PATHS = {"/api/auth/login"}

    def __init__(self, app):
        super().__init__(app)
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._last_cleanup = 0.0

    @staticmethod
    def _client_key(request: Request) -> str:
        credential = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
        if credential:
            return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:24]
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _limit_for_path(path: str) -> tuple[str, int]:
        if path in RateLimitMiddleware._UPLOAD_PATHS:
            return "upload", config.rate_limit_upload_per_minute
        if path in RateLimitMiddleware._LOGIN_PATHS:
            return "login", config.rate_limit_login_per_minute
        if path in RateLimitMiddleware._EXPENSIVE_PATHS:
            return "expensive", config.rate_limit_expensive_per_minute
        return "default", config.rate_limit_default_per_minute

    async def dispatch(self, request: Request, call_next):
        if not config.rate_limit_enabled or not request.url.path.startswith("/api/"):
            return await call_next(request)

        bucket_name, limit = self._limit_for_path(request.url.path)
        if limit <= 0:
            return JSONResponse(status_code=503, content={"detail": "Endpoint is disabled"})

        now = time.monotonic()
        cutoff = now - 60.0
        key = (self._client_key(request), bucket_name)
        async with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(60 - (now - bucket[0])))
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests"},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

            if now - self._last_cleanup > 60:
                stale = [
                    bucket_key
                    for bucket_key, values in self._requests.items()
                    if not values or values[-1] <= cutoff
                ]
                for bucket_key in stale:
                    self._requests.pop(bucket_key, None)
                self._last_cleanup = now

        return await call_next(request)
