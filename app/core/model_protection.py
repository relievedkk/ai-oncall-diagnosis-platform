"""Per-process backpressure, provider cooldown and observed usage metadata."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.callbacks import AsyncCallbackHandler
from starlette.responses import JSONResponse

from app.config import config
from app.core.data_safety import reserve_model_input


class ProviderUnavailable(RuntimeError):
    pass


class ProviderCircuit:
    def __init__(self):
        self.open_until = 0.0

    def ensure_available(self):
        if time.monotonic() < self.open_until:
            raise ProviderUnavailable("模型服务暂时限额，请稍后重试")

    def record_failure(self, error):
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if getattr(error, "status_code", None) == 429:
                self.open_until = time.monotonic() + config.provider_cooldown_seconds
                return
            error = getattr(error, "__cause__", None) or getattr(error, "__context__", None)


provider_circuit = ProviderCircuit()


@dataclass
class UsageCapture:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    calls_with_usage: int = 0


_capture = ContextVar("model_usage_capture", default=None)


@contextmanager
def capture_usage(capture):
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


class ModelObserver(AsyncCallbackHandler):
    raise_error = True
    run_inline = True

    async def on_chat_model_start(self, serialized, messages, **kwargs):
        provider_circuit.ensure_available()
        await reserve_model_input(messages, kwargs.get("invocation_params"))
        capture = _capture.get()
        if capture is not None:
            capture.calls += 1

    async def on_llm_end(self, response, **kwargs):
        capture = _capture.get()
        if capture is None:
            return
        found = False
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    capture.input_tokens += usage.get("input_tokens", 0)
                    capture.output_tokens += usage.get("output_tokens", 0)
                    found = True
        if found:
            capture.calls_with_usage += 1

    async def on_llm_error(self, error, **kwargs):
        provider_circuit.record_failure(error)


class ModelAdmissionMiddleware:
    """Hold the permit through the entire ASGI response, including SSE streams."""

    def __init__(self, app):
        self.app = app
        self.limit = config.chat_max_concurrent
        self.capacity = self.limit + config.chat_max_waiting
        self.semaphore = asyncio.Semaphore(self.limit)
        self.admitted = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in {"/api/chat", "/api/chat_stream"}:
            return await self.app(scope, receive, send)
        if self.admitted >= self.capacity:
            return await JSONResponse(
                status_code=429,
                content={"detail": "服务繁忙，请稍后重试"},
                headers={"Retry-After": "2"},
            )(scope, receive, send)
        self.admitted += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), config.chat_queue_timeout_seconds)
                acquired = True
            except TimeoutError:
                return await JSONResponse(
                    status_code=429,
                    content={"detail": "排队超时，请稍后重试"},
                    headers={"Retry-After": "2"},
                )(scope, receive, send)
            await self.app(scope, receive, send)
        finally:
            if acquired:
                self.semaphore.release()
            self.admitted -= 1
