import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_admission_rejects_excess_and_releases_after_cancellation(monkeypatch):
    from app.config import config
    from app.core.model_protection import ModelAdmissionMiddleware

    monkeypatch.setattr(config, "chat_max_concurrent", 1)
    monkeypatch.setattr(config, "chat_max_waiting", 0)
    app = FastAPI()
    app.add_middleware(ModelAdmissionMiddleware)
    started = asyncio.Event()

    @app.post("/api/chat")
    async def chat():
        started.set()
        await asyncio.sleep(30)
        return {}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        first = asyncio.create_task(c.post("/api/chat"))
        await started.wait()
        assert (await c.post("/api/chat")).status_code == 429
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        next_request = asyncio.create_task(c.post("/api/chat"))
        await asyncio.sleep(0.01)
        assert not next_request.done()
        next_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_request


def test_provider_circuit_opens_only_for_429_and_expires(monkeypatch):
    from app.core.model_protection import ProviderCircuit, ProviderUnavailable

    clock = [10.0]
    monkeypatch.setattr("app.core.model_protection.time.monotonic", lambda: clock[0])
    circuit = ProviderCircuit()

    class QuotaError(Exception):
        status_code = 429

    circuit.record_failure(QuotaError())
    with pytest.raises(ProviderUnavailable):
        circuit.ensure_available()
    clock[0] += 1000
    circuit.ensure_available()


@pytest.mark.asyncio
async def test_usage_callback_captures_provider_metadata():
    from app.core.model_protection import ModelObserver, UsageCapture, capture_usage

    capture = UsageCapture()
    result = SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 3})
                )
            ]
        ]
    )
    with capture_usage(capture):
        await ModelObserver().on_llm_end(result, run_id="one")
    assert capture.input_tokens == 12 and capture.output_tokens == 3
