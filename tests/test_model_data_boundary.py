import importlib
import json

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda

from app.core.llm_factory import llm_factory


def test_provider_payload_redacts_nested_and_free_text_credentials():
    model = llm_factory.create_chat_model(api_key="offline-test-key", streaming=False)
    message = HumanMessage(
        content=json.dumps(
            {
                "service": "checkout-api",
                "value": 96,
                "Headers": {"Authorization": "Bearer SYNTHETIC_PROVIDER_SECRET"},
                "db_password": "SYNTHETIC_DB_SECRET",
                "message": "api_key=SYNTHETIC_TEXT_SECRET",
            }
        )
    )
    payload = model._get_request_payload([message])
    serialized = json.dumps(payload)
    assert "SYNTHETIC_PROVIDER_SECRET" not in serialized
    assert "SYNTHETIC_DB_SECRET" not in serialized
    assert "SYNTHETIC_TEXT_SECRET" not in serialized
    assert "checkout-api" in serialized and "96" in serialized
    assert model.max_retries == 0  # Hidden HTTP retries must not bypass call accounting.


def test_provider_rejects_oversized_input_before_transport():
    model = llm_factory.create_chat_model(api_key="offline-test-key", streaming=False)
    with pytest.raises(ValueError, match="模型.*上限"):
        model._get_request_payload([HumanMessage(content="X" * 70_000)])


@pytest.mark.asyncio
async def test_report_model_receives_bounded_redacted_evidence():
    module = importlib.import_module("app.agent.aiops.replanner")
    observed = []

    async def respond(value):
        observed.extend(value.to_messages())
        return module.Response(response="offline report")

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)

    await module._generate_response(
        {
            "input": "diagnose checkout-api",
            "past_steps": [],
            "evidence": [
                {
                    "source": "query_service_logs",
                    "result": {
                        "Authorization": "Bearer SYNTHETIC_REPORT_SECRET",
                        "message": "X" * 100_000,
                    },
                }
            ],
        },
        Model(),
    )
    content = "\n".join(str(message.content) for message in observed)
    assert "SYNTHETIC_REPORT_SECRET" not in content
    assert len(content.encode()) < 65_536
    assert "truncated" in content.lower()


def test_tool_result_boundary_preserves_small_evidence_and_marks_large_results():
    from app.core.data_safety import bounded_tool_result

    small = bounded_tool_result(
        {"service": "checkout-api", "value": 96, "access_token": "SYNTHETIC_TOOL_SECRET"}
    )
    assert small["value"] == 96 and small["service"] == "checkout-api"
    assert "SYNTHETIC_TOOL_SECRET" not in json.dumps(small)
    large = bounded_tool_result({"message": "X" * 100_000})
    assert large["truncated"] is True and large["success"] is False
    assert len(json.dumps(large).encode()) <= 16_384


@pytest.mark.asyncio
async def test_diagnosis_budget_survives_context_reentry_and_rejects_before_next_call():
    from app.core.data_safety import ModelBudgetExceeded, diagnosis_budget_scope
    from app.core.model_protection import ModelObserver

    class Ledger:
        calls = 0

        async def reserve_model_budget(self, diagnosis_id, **kwargs):
            if self.calls >= 2:
                return False
            self.calls += 1
            return True

    ledger = Ledger()
    observer = ModelObserver()
    for _ in range(2):
        with diagnosis_budget_scope(ledger, "synthetic-job", "execution-owner"):
            await observer.on_chat_model_start({}, [[HumanMessage(content="diagnose")]])
    with diagnosis_budget_scope(ledger, "synthetic-job", "execution-owner"):
        with pytest.raises(ModelBudgetExceeded):
            await observer.on_chat_model_start({}, [[HumanMessage(content="third call")]])
    assert ledger.calls == 2


def test_tool_message_cannot_persist_unredacted_artifact_or_oversized_content():
    from langchain_core.messages import ToolMessage

    from app.core.data_safety import sanitize_tool_message

    original = ToolMessage(
        content='{"password":"SYNTHETIC_CONTENT_SECRET","value":96}',
        tool_call_id="audit-call",
        artifact={"password": "SYNTHETIC_ARTIFACT_SECRET"},
    )
    safe = sanitize_tool_message(original)
    assert "SYNTHETIC_CONTENT_SECRET" not in str(safe.content)
    assert safe.artifact is None
    assert safe.tool_call_id == original.tool_call_id
    huge = sanitize_tool_message(ToolMessage(content="X" * 100_000, tool_call_id="huge"))
    assert json.loads(huge.content)["truncated"] is True


def test_result_boundary_rejects_invalid_configured_budget():
    from app.core.data_safety import bounded_tool_result

    with pytest.raises(ValueError):
        bounded_tool_result("small", max_bytes=0)


@pytest.mark.asyncio
async def test_transport_boundary_and_durable_budget_cover_tool_and_structured_models(monkeypatch):
    import httpx
    from pydantic import BaseModel

    from app.config import config
    from app.core.data_safety import ModelBudgetExceeded, diagnosis_budget_scope
    from app.core.llm_factory import SafeChatOpenAI
    from app.core.model_protection import ModelObserver
    from app.repositories.diagnosis import InMemoryDiagnosisRepository

    monkeypatch.setattr(config, "diagnosis_max_model_calls", 2)
    sent = []

    class Summary(BaseModel):
        answer: str

    def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        message = {"role": "assistant", "content": "safe report"}
        if body.get("tools", [{}])[0].get("function", {}).get("name") == "Summary":
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "synthetic-call", "type": "function", "function": {
                    "name": "Summary", "arguments": '{"answer":"safe report"}',
                },
            }]}
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion",
            "created": 0, "model": "synthetic-model", "choices": [{"index": 0,
                "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job({"diagnosis_id": "budget-transport", "event_key": "budget-transport", "trigger": "manual", "status": "queued"})
    await repository.claim_job("budget-transport", "task", "worker", execution_token="first")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
        model = SafeChatOpenAI(model="synthetic-model", api_key="offline-key", base_url="https://model.invalid/v1",
                              http_async_client=client, max_retries=0, callbacks=[ModelObserver()])
        with diagnosis_budget_scope(repository, "budget-transport", "first"):
            await model.bind_tools([{"name": "noop", "description": "offline tool", "parameters": {"type": "object", "properties": {}}}]).ainvoke(
                [HumanMessage(content='Authorization: Bearer SYNTHETIC_TRANSPORT_SECRET')])
        await repository.prepare_retry("budget-transport", "synthetic retry", execution_token="first")
        await repository.claim_job("budget-transport", "task", "worker", execution_token="second")
        with diagnosis_budget_scope(repository, "budget-transport", "second"):
            result = await model.with_structured_output(Summary, method="function_calling").ainvoke([HumanMessage(content="report")])
            assert result.answer == "safe report"
            with pytest.raises(ModelBudgetExceeded):
                await model.ainvoke([HumanMessage(content="budget exhausted")])
    assert len(sent) == 2
    assert "SYNTHETIC_TRANSPORT_SECRET" not in json.dumps(sent)
    assert (await repository.get_job("budget-transport"))["model_calls"] == 2


@pytest.mark.parametrize("terminated", [True, False], ids=["complete-pem", "incomplete-pem"])
def test_tool_preview_redacts_private_keys_before_truncating(terminated):
    from app.core.data_safety import bounded_tool_result

    value = "service=checkout-api\n-----BEGIN PRIVATE KEY-----\nSYNTHETIC_PEM_SECRET\n" + "A" * 20_000
    if terminated:
        value += "\n-----END PRIVATE KEY-----"
    result = bounded_tool_result(value)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC_PEM_SECRET" not in serialized
    assert "service=checkout-api" in serialized
    assert "[REDACTED]" in serialized
    assert len(serialized.encode()) <= 16_384


@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["escaped-unicode", "unicode"])
def test_large_json_tool_preview_redacts_cookie_and_nested_secrets_before_truncating(ensure_ascii):
    from app.core.data_safety import bounded_tool_result

    value = json.dumps({
        "service": "checkout-api", "value": 96,
        "headers": {"Cookie": "SYNTHETIC_COOKIE_SECRET"},
        "nested": [{"api_key": "SYNTHETIC_NESTED_SECRET"}],
        "message": '\\"故障' * 20_000,
    }, ensure_ascii=ensure_ascii)
    result = bounded_tool_result(value)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC_COOKIE_SECRET" not in serialized
    assert "SYNTHETIC_NESTED_SECRET" not in serialized
    assert result["truncated"] is True and result["success"] is False
    assert result["error"] == "tool_result_truncated"
    assert "checkout-api" in serialized and "96" in serialized
    assert len(serialized.encode()) <= 16_384


def test_token_suffix_credentials_are_redacted_without_removing_usage_counts(monkeypatch):
    from app.config import config
    from app.core.data_safety import _text_redactor, redact_sensitive

    monkeypatch.setattr(config, "mcp_cls_token", "SYNTHETIC_CONFIG_TOKEN_SECRET")
    _text_redactor.cache_clear()
    try:
        result = redact_sensitive({
            "execution_token": "SYNTHETIC_EXECUTION_SECRET",
            "rollback_token": "SYNTHETIC_ROLLBACK_SECRET",
            "input_tokens": 123, "output_tokens": 45, "token_count": 168,
            "message": "configured credential: SYNTHETIC_CONFIG_TOKEN_SECRET",
        })
        assert "SYNTHETIC_" not in json.dumps(result)
        assert result["input_tokens"] == 123 and result["output_tokens"] == 45
        assert result["token_count"] == 168
    finally:
        _text_redactor.cache_clear()


@pytest.mark.asyncio
async def test_large_pydantic_json_schema_is_rejected_before_http_transport():
    import httpx
    from pydantic import Field, create_model

    from app.core.data_safety import ModelInputRejected
    from app.core.llm_factory import SafeChatOpenAI

    schema = create_model("HugeSchema", answer=(str, Field(description="X" * 100_000)))
    sent = []

    def transport(request):
        sent.append(request.content)
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion",
            "created": 0, "model": "synthetic-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": '{"answer":"safe report"}'},
                "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
        model = SafeChatOpenAI(model="synthetic-model", api_key="offline-key",
                              base_url="https://model.invalid/v1", http_async_client=client, max_retries=0)
        with pytest.raises(ModelInputRejected):
            await model.with_structured_output(schema, method="json_schema").ainvoke("report")
    assert sent == []


@pytest.mark.asyncio
async def test_json_schema_bytes_consume_durable_budget_across_retry(monkeypatch):
    import httpx
    from pydantic import Field, create_model

    from app.config import config
    from app.core.data_safety import ModelBudgetExceeded, diagnosis_budget_scope
    from app.core.llm_factory import SafeChatOpenAI
    from app.core.model_protection import ModelObserver
    from app.repositories.diagnosis import InMemoryDiagnosisRepository

    monkeypatch.setattr(config, "diagnosis_max_model_calls", 10)
    monkeypatch.setattr(config, "diagnosis_max_model_input_bytes", 5_000)
    schema = create_model("SummaryWithCredentialField",
                          execution_token=(str, Field(description="context " * 350)))
    sent = []

    def transport(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion",
            "created": 0, "model": "synthetic-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": '{"execution_token":"parsed answer"}'},
                "finish_reason": "stop"}]})

    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job({"diagnosis_id": "schema-budget", "event_key": "schema-budget", "trigger": "manual", "status": "queued"})
    await repository.claim_job("schema-budget", "task", "worker", execution_token="first")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
        model = SafeChatOpenAI(model="synthetic-model", api_key="offline-key", base_url="https://model.invalid/v1",
                              http_async_client=client, max_retries=0, callbacks=[ModelObserver()])
        structured = model.with_structured_output(schema, method="json_schema")
        with diagnosis_budget_scope(repository, "schema-budget", "first"):
            result = await structured.ainvoke("report")
        assert result.execution_token == "parsed answer"
        first = await repository.get_job("schema-budget")
        assert first["model_input_bytes"] >= 2_800
        assert sent[0]["response_format"]["json_schema"]["schema"]["properties"]["execution_token"]["type"] == "string"
        await repository.prepare_retry("schema-budget", "synthetic retry", execution_token="first")
        await repository.claim_job("schema-budget", "task", "worker", execution_token="second")
        with diagnosis_budget_scope(repository, "schema-budget", "second"):
            with pytest.raises(ModelBudgetExceeded):
                await structured.ainvoke("report")
    assert len(sent) == 1
    assert (await repository.get_job("schema-budget"))["model_calls"] == 1


@pytest.mark.asyncio
async def test_legacy_function_schema_is_included_in_model_input_budget(monkeypatch):
    from app.config import config
    from app.core.data_safety import ModelInputRejected, reserve_model_input

    monkeypatch.setattr(config, "model_max_input_bytes", 1_024)
    with pytest.raises(ModelInputRejected):
        await reserve_model_input([[HumanMessage(content="report")]], {
            "functions": [{"name": "lookup", "description": "X" * 2_000,
                           "parameters": {"type": "object", "properties": {}}}],
        })
