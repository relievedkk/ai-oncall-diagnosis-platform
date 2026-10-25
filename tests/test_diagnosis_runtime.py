"""Evaluation dependencies never replace another task's production dependencies."""

import asyncio
import os
import subprocess
import sys

import pytest


async def test_concurrent_contexts_isolate_tools_models_and_restore_defaults():
    from app.agent.aiops.runtime import (
        DiagnosisRuntime,
        create_model,
        current_runtime,
        diagnosis_runtime,
        resolve_tools,
    )

    async def forbidden():
        raise AssertionError("production tools must not be initialized")

    async def run(name):
        with diagnosis_runtime(
            DiagnosisRuntime(
                context="evaluation",
                task_scope=name,
                tools=[name],
                model_factory=lambda **kwargs: (name, kwargs),
            )
        ):
            await asyncio.sleep(0)
            return (
                await resolve_tools(forbidden),
                create_model(temperature=0),
                current_runtime().task_scope,
            )

    a, b = await asyncio.gather(run("A"), run("B"))
    assert a == (["A"], ("A", {"temperature": 0}), "A")
    assert b == (["B"], ("B", {"temperature": 0}), "B")
    assert current_runtime().context == "production"


def test_import_graph_without_key_does_not_initialize_embeddings_or_mcp():
    environment = {**os.environ, "DASHSCOPE_API_KEY": ""}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.services.aiops_service; "
            'assert "app.services.vector_embedding_service" not in sys.modules; '
            'assert "app.main" not in sys.modules',
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_evaluation_event_cannot_submit_remediation():
    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.services.aiops_service import AIOpsService

    with diagnosis_runtime(DiagnosisRuntime(context="replay", task_scope="synthetic")):
        event = AIOpsService()._format_replanner_event(
            {
                "response": "partial",
                "remediation": {
                    "action_type": "restart_instance",
                    "service_name": "orders",
                    "instance_id": "o1",
                },
            }
        )
    assert "remediation" not in event


def test_runtime_rejects_model_controlled_context():
    from app.agent.aiops.runtime import DiagnosisRuntime

    with pytest.raises(ValueError):
        DiagnosisRuntime(context="unrestricted")


async def test_live_evaluation_does_not_acquire_production_database_quota(monkeypatch):
    from langchain_openai import ChatOpenAI

    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.config import config
    from app.core.llm_factory import SafeChatOpenAI
    from app.services import shared_model_quota

    def forbidden(*args, **kwargs):
        raise AssertionError("live evaluation cannot access production quota database")

    async def provider(*args, **kwargs):
        return "isolated-provider-response"

    monkeypatch.setattr(config, "shared_model_quota_enabled", True)
    monkeypatch.setattr(shared_model_quota, "shared_transport", forbidden)
    monkeypatch.setattr(ChatOpenAI, "_agenerate_with_cache", provider)
    model = SafeChatOpenAI(api_key="test-only", model="test")
    with diagnosis_runtime(DiagnosisRuntime(context="evaluation")):
        assert await model._agenerate_with_cache([]) == "isolated-provider-response"
