import pytest

from app.services.rag_agent_service import (
    ASSISTANT_IDENTITY,
    RagAgentService,
    identity_response,
)


@pytest.mark.parametrize(
    "question",
    ["你是谁？", "你叫什么名字", "介绍一下你自己。", "你是什么模型？"],
)
def test_identity_questions_use_product_identity(question: str) -> None:
    assert identity_response(question) == ASSISTANT_IDENTITY


def test_normal_questions_are_not_intercepted() -> None:
    assert identity_response("如何排查 CPU 告警？") is None


def test_system_prompt_forbids_provider_identity() -> None:
    prompt = RagAgentService().system_prompt
    assert "智能 OnCall 助手" in prompt
    assert "不得自称通义千问" in prompt


@pytest.mark.asyncio
async def test_query_returns_identity_without_calling_model(monkeypatch) -> None:
    service = RagAgentService()

    async def fail_initialize() -> None:
        raise AssertionError("identity response must not call the model")

    monkeypatch.setattr(service, "_initialize_agent", fail_initialize)
    assert await service.query("你是谁？", "identity-test") == ASSISTANT_IDENTITY


@pytest.mark.asyncio
async def test_stream_query_returns_identity_without_calling_model(monkeypatch) -> None:
    service = RagAgentService()

    async def fail_initialize() -> None:
        raise AssertionError("identity response must not call the model")

    monkeypatch.setattr(service, "_initialize_agent", fail_initialize)
    events = [event async for event in service.query_stream("你是谁？", "identity-stream")]

    assert events == [
        {"type": "content", "data": ASSISTANT_IDENTITY, "node": "identity"},
        {"type": "complete"},
    ]
