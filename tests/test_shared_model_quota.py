"""Offline quota policy and actual SDK transport lifecycle regressions."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError


def quota_module():
    return importlib.import_module("app.services.shared_model_quota")


def test_default_quota_and_stable_paths(tmp_path, monkeypatch):
    from app.config import Settings

    settings = Settings(_env_file=None, oncall_data_root=str(tmp_path / "data"),
                        oncall_config_root=str(tmp_path / "configuration"))
    assert settings.shared_model_quota_enabled is False
    assert settings.shared_model_quota_concurrency == 4
    assert settings.shared_model_quota_calls_per_minute == 60
    assert settings.shared_model_quota_input_bytes_per_minute == 1_048_576
    assert settings.upload_dir == tmp_path / "data" / "uploads"
    assert settings.resolve_config_path("config/service_catalog.json") == tmp_path / "configuration" / "config" / "service_catalog.json"
    monkeypatch.chdir(tmp_path)
    assert settings.upload_dir == tmp_path / "data" / "uploads"
    assert Settings(_env_file=None).upload_dir == tmp_path / "uploads"


@pytest.mark.parametrize("values", [
    {"shared_model_quota_scope": ""}, {"shared_model_quota_concurrency": 0},
    {"shared_model_quota_calls_per_minute": 0},
    {"shared_model_quota_input_bytes_per_minute": 0},
    {"shared_model_quota_lease_seconds": 120},
    {"shared_model_quota_renew_seconds": 150},
    {"shared_model_quota_scope": "provider secret/account"},
])
def test_invalid_quota_configuration_rejected(values):
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_window_charges_failures_and_resets_only_at_database_minute_boundary():
    module = importlib.import_module("app.repositories.model_quota")
    now = datetime(2026, 10, 24, 12, 0, 59, tzinfo=UTC)
    state = SimpleNamespace(window_started_at=now.replace(second=0), calls=0,
                            input_bytes=0, cooldown_until=None)
    limits = module.QuotaLimits(concurrency=1, calls_per_minute=2,
                                input_bytes_per_minute=10, lease_seconds=150)
    module.reserve_window(state, [], now, limits, 6)
    with pytest.raises(module.SharedQuotaExceeded):
        module.reserve_window(state, [], now, limits, 5)
    assert (state.calls, state.input_bytes) == (1, 6)
    module.reserve_window(state, [], now, limits, 4)
    with pytest.raises(module.SharedQuotaExceeded) as error:
        module.reserve_window(state, [], now, limits, 0)
    assert error.value.retry_after > 0
    module.reserve_window(state, [], now + timedelta(seconds=1), limits, 10)
    assert (state.calls, state.input_bytes) == (1, 10)


def test_active_leases_expiry_and_cooldown_use_database_time():
    module = importlib.import_module("app.repositories.model_quota")
    now = datetime(2026, 10, 24, tzinfo=UTC)
    state = SimpleNamespace(window_started_at=now, calls=0, input_bytes=0,
                            cooldown_until=None)
    limits = module.QuotaLimits(1, 60, 1_048_576, 150)
    with pytest.raises(module.SharedQuotaExceeded):
        module.reserve_window(state, [now + timedelta(seconds=2)], now, limits, 1)
    module.reserve_window(state, [now], now, limits, 1)
    state.cooldown_until = now + timedelta(seconds=30)
    with pytest.raises(module.SharedQuotaExceeded) as error:
        module.reserve_window(state, [], now, limits, 1)
    assert error.value.retry_after == 30


class MemoryLeases:
    """Lifecycle fault fixture; database serialization is tested only on PostgreSQL."""

    def __init__(self):
        self.active = set()
        self.reservations = []
        self.released = []
        self.cooldowns = []
        self.renewed = asyncio.Event()
        self.fail_renew = False

    async def reserve(self, scope, input_bytes, limits, *, call_id=None):
        token = call_id or str(len(self.reservations))
        self.reservations.append(input_bytes)
        self.active.add(token)
        return token

    async def renew(self, scope, token, lease_seconds):
        self.renewed.set()
        if self.fail_renew:
            raise OSError("synthetic database loss")
        return token in self.active

    async def release(self, scope, token):
        self.released.append(token)
        self.active.discard(token)

    async def cooldown(self, scope, seconds):
        self.cooldowns.append(seconds)


def guard(repository, **kwargs):
    return quota_module().SharedModelQuota(repository, scope="synthetic-account", renew_seconds=0.01, **kwargs)


@pytest.mark.asyncio
async def test_guard_releases_on_failure_cancellation_and_does_not_refund_minute_charge():
    repository = MemoryLeases()
    service = guard(repository)
    with pytest.raises(ValueError):
        async with service.hold(100):
            raise ValueError("synthetic provider failure")
    entered = asyncio.Event()

    async def call():
        async with service.hold(200):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(call())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not repository.active
    assert repository.reservations == [100, 200]
    assert len(set(repository.released)) == 2


@pytest.mark.asyncio
async def test_renewal_failure_cancels_transport_and_closes_guard_to_future_calls():
    repository = MemoryLeases()
    repository.fail_renew = True
    service = guard(repository)
    cancelled = asyncio.Event()
    with pytest.raises(quota_module().SharedQuotaUnavailable):
        async with service.hold(20):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    assert cancelled.is_set() and not repository.active
    with pytest.raises(quota_module().SharedQuotaUnavailable):
        async with service.hold(20):
            pytest.fail("lost renewal must close subsequent calls")
    assert len(repository.reservations) == 1


@pytest.mark.asyncio
async def test_shared_429_and_reservation_database_failure_fail_closed():
    repository = MemoryLeases()
    service = guard(repository)

    class Limited(RuntimeError):
        status_code = 429

    with pytest.raises(Limited):
        async with service.hold(20):
            raise Limited()
    assert repository.cooldowns == [30.0]

    async def broken(*args, **kwargs):
        raise OSError("secret database connection")

    repository.reserve = broken
    with pytest.raises(quota_module().SharedQuotaUnavailable, match="共享") as error:
        async with service.hold(20):
            pytest.fail("must not reach provider")
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_sdk_tool_structured_generate_stream_and_sync_boundary(monkeypatch):
    import json

    from pydantic import BaseModel

    from app.config import config
    from app.core.llm_factory import SafeChatOpenAI

    repository = MemoryLeases()
    service = guard(repository)
    monkeypatch.setattr(config, "shared_model_quota_enabled", True)
    monkeypatch.setattr("app.services.shared_model_quota.get_shared_model_quota", lambda: service)
    sent = []

    class Summary(BaseModel):
        answer: str

    async def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        assert len(repository.active) == 1
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                b'data: {"id":"s","object":"chat.completion.chunk","created":0,"model":"offline","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
                b'data: {"id":"s","object":"chat.completion.chunk","created":0,"model":"offline","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n'))
        return httpx.Response(200, json={"id": "r", "object": "chat.completion", "created": 0,
            "model": "offline", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"answer":"ok"}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
        model = SafeChatOpenAI(model="offline", api_key="offline-key", max_retries=0,
                              base_url="https://model.invalid/v1", http_async_client=client)
        await model.bind_tools([{"name": "lookup", "parameters": {"type": "object", "properties": {}}}]).ainvoke(
            [HumanMessage(content="Authorization: Bearer SYNTHETIC_SECRET")])
        assert (await model.with_structured_output(Summary, method="json_schema").ainvoke("report")).answer == "ok"
        assert "".join([chunk.content async for chunk in model.astream("stream")]) == "ok"
        with pytest.raises(RuntimeError, match="异步|async"):
            model.invoke("sync must be disabled")
    assert len(repository.reservations) == len(sent) == 3
    assert not repository.active
    assert "SYNTHETIC_SECRET" not in json.dumps(sent)
    assert repository.reservations[1] > repository.reservations[2]


@pytest.mark.asyncio
async def test_sdk_stream_early_close_releases_immediately(monkeypatch):
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langchain_openai import ChatOpenAI

    from app.config import config
    from app.core.llm_factory import SafeChatOpenAI

    repository = MemoryLeases()
    service = guard(repository)
    closed = asyncio.Event()

    async def streaming(*args, **kwargs):
        try:
            yield ChatGenerationChunk(message=AIMessageChunk(content="first"))
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(ChatOpenAI, "_astream", streaming)
    monkeypatch.setattr(config, "shared_model_quota_enabled", True)
    monkeypatch.setattr("app.services.shared_model_quota.get_shared_model_quota", lambda: service)
    model = SafeChatOpenAI(model="offline", api_key="offline-key", max_retries=0)
    stream = model.astream("report")
    assert (await anext(stream)).content == "first"
    await stream.aclose()
    assert not repository.active
    assert closed.is_set()


@pytest.mark.asyncio
async def test_sdk_provider_hard_deadline_releases_permit(monkeypatch):
    from langchain_openai import ChatOpenAI

    from app.config import config
    from app.core.llm_factory import SafeChatOpenAI

    repository = MemoryLeases()
    service = guard(repository)

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ChatOpenAI, "_agenerate", slow)
    monkeypatch.setattr(config, "llm_request_timeout", 0.02)
    monkeypatch.setattr(config, "shared_model_quota_enabled", True)
    monkeypatch.setattr("app.services.shared_model_quota.get_shared_model_quota", lambda: service)
    model = SafeChatOpenAI(model="offline", api_key="offline-key", max_retries=0)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(model.ainvoke("report"), 0.2)
    assert asyncio.get_running_loop().time() - started < 0.15
    assert not repository.active
    assert repository.renewed.is_set()


def test_direct_model_constructor_disables_hidden_sdk_retries():
    from app.core.llm_factory import SafeChatOpenAI

    model = SafeChatOpenAI(model="offline", api_key="offline-key", max_retries=5)
    assert model.max_retries == 0
    assert model.root_async_client.max_retries == 0


@pytest.mark.asyncio
async def test_reservation_finishing_after_renewal_loss_cannot_start_provider():
    repository = MemoryLeases()
    service = guard(repository)
    reserving = asyncio.Event()
    finish = asyncio.Event()
    reserve = repository.reserve

    async def delayed(*args, **kwargs):
        reserving.set()
        await finish.wait()
        return await reserve(*args, **kwargs)

    repository.reserve = delayed

    async def call():
        with pytest.raises(quota_module().SharedQuotaUnavailable):
            async with service.hold(20):
                pytest.fail("late reservation must not start transport")

    task = asyncio.create_task(call())
    await reserving.wait()
    service.broken = True
    finish.set()
    await task
    assert not repository.active


@pytest.mark.asyncio
async def test_transport_cancellation_and_sibling_tasks_reserve_separately(monkeypatch):
    from langchain_openai import ChatOpenAI

    from app.config import config
    from app.core.llm_factory import SafeChatOpenAI

    repository = MemoryLeases()
    service = guard(repository)
    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        if len(repository.active) == 2:
            entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ChatOpenAI, "_agenerate", slow)
    monkeypatch.setattr(config, "shared_model_quota_enabled", True)
    monkeypatch.setattr("app.services.shared_model_quota.get_shared_model_quota", lambda: service)
    model = SafeChatOpenAI(model="offline", api_key="offline-key", max_retries=0)
    tasks = [asyncio.create_task(model.ainvoke("report")) for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 1)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert len(repository.reservations) == 2 and not repository.active
