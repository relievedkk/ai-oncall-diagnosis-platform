import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage

from app.agent.aiops.single_pass import run_single_pass
from app.evaluation.single_pass_hypotheses import rank_hypotheses
from app.evaluation.single_pass_journal import AttemptJournal
from app.evaluation.single_pass_packet import build_packet
from tests.single_pass_helpers import choice_of as decision
from tests.test_single_pass_hypotheses import evidence


class Model:
    max_retries = 0

    def __init__(self, result=None, error=None, delay=0):
        self.calls = 0
        self.result, self.error, self.delay = result, error, delay

    def with_structured_output(self, schema, **kwargs):
        assert kwargs["include_raw"] is True
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            ),
            "parsed": self.result,
            "parsing_error": ValueError("SECRET malformed response")
            if self.result is None
            else None,
        }


def packet():
    e = evidence("cpu")
    return build_packet(e, rank_hypotheses(e))


def test_concurrent_reservation_and_restart_never_reset_attempt(tmp_path):
    db = tmp_path / "attempts.sqlite"

    def reserve(_):
        journal = AttemptJournal(db)
        try:
            return journal.reserve("experiment", "case", "fingerprint")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    assert sum(r is not None for r in results) == 1
    with pytest.raises(ValueError, match="attempt_already_reserved"):
        AttemptJournal(db).reserve("experiment", "case", "fingerprint")
    with pytest.raises(ValueError, match="configuration_mismatch"):
        AttemptJournal(db).reserve("experiment", "case", "changed")


@pytest.mark.asyncio
async def test_completed_result_reused_only_on_explicit_resume(tmp_path):
    p = packet()
    model = Model(decision(p))

    async def run(resume=False, config=None):
        return await run_single_pass(
            p,
            journal=AttemptJournal(tmp_path / "journal.sqlite"),
            experiment_id="run",
            config=config or {"model": "test"},
            model_factory=lambda: model,
            resume=resume,
        )

    first = await run()
    assert first["model_calls"] == 1 and first["usage"]["total_tokens"] == 25
    second = await run(resume=True)
    assert second["reused"] and model.calls == 1
    with pytest.raises(ValueError, match="explicit_resume_required"):
        await run()
    with pytest.raises(ValueError, match="configuration_mismatch"):
        await run(resume=True, config={"model": "changed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["parse", "provider", "timeout", "cancel"])
async def test_failures_never_retry_and_parse_usage_is_preserved(tmp_path, mode):
    p = packet()
    model = Model(
        error=RuntimeError("SECRET")
        if mode == "provider"
        else asyncio.CancelledError()
        if mode == "cancel"
        else None,
        delay=0.1 if mode == "timeout" else 0,
    )
    journal = AttemptJournal(tmp_path / "journal.sqlite")
    kwargs = {
        "journal": journal,
        "experiment_id": "run",
        "config": {"model": "test"},
        "model_factory": lambda: model,
        "timeout_seconds": 0.01 if mode == "timeout" else 1,
    }
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run_single_pass(p, **kwargs)
    else:
        result = await run_single_pass(p, **kwargs)
        assert result["status"] == "error" and "SECRET" not in str(result)
        if mode == "parse":
            assert result["usage"]["total_tokens"] == 25
    with pytest.raises(ValueError, match="attempt_already_reserved"):
        await run_single_pass(p, resume=True, **kwargs)
    assert model.calls == 1


@pytest.mark.asyncio
async def test_model_setup_failure_does_not_reserve_or_call(tmp_path):
    p = packet()
    journal = AttemptJournal(tmp_path / "journal.sqlite")
    model = Model(decision(p))
    model.max_retries = 2
    with pytest.raises(ValueError, match="hidden_retries_forbidden"):
        await run_single_pass(
            p, journal=journal, experiment_id="run", config={}, model_factory=lambda: model
        )
    assert model.calls == 0
    assert journal.load("run", p.case_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "malformed", "provider_error"])
async def test_real_sdk_transport_makes_exactly_one_mock_http_request(tmp_path, mode):
    import httpx

    from app.core.llm_factory import SafeChatOpenAI

    p = packet()
    requests = []

    def handler(request):
        requests.append(request)
        if mode == "provider_error":
            return httpx.Response(500, json={"error": {"message": "synthetic failure"}})
        arguments = decision(p).model_dump_json() if mode == "ok" else "{malformed"
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {
                                        "name": "SinglePassChoice",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = SafeChatOpenAI(
            model="mock",
            api_key="test-placeholder",
            base_url="https://mock.invalid/v1",
            http_async_client=client,
            max_tokens=384,
        )
        result = await run_single_pass(
            p,
            journal=AttemptJournal(tmp_path / "journal.sqlite"),
            experiment_id="mock-http",
            config={"model": "mock"},
            model_factory=lambda: model,
        )
    assert len(requests) == 1
    assert result["model_calls"] == 1
    assert (result["status"] == "supported") is (mode == "ok")
    if mode != "provider_error":
        assert result["usage"]["total_tokens"] == 25
