"""Context-local dependencies for production graphs and isolated evaluations."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiagnosisRuntime:
    context: str = "production"
    task_scope: str = ""
    target_service: str = ""
    tools: list | None = None
    model_factory: Any = None
    knowledge_lookup: Any = None

    def __post_init__(self):
        if self.context not in {"production", "replay", "evaluation"}:
            raise ValueError("Invalid diagnosis runtime context")


_runtime: ContextVar[DiagnosisRuntime | None] = ContextVar("diagnosis_runtime", default=None)


def current_runtime() -> DiagnosisRuntime:
    return _runtime.get() or DiagnosisRuntime()


@contextmanager
def diagnosis_runtime(runtime: DiagnosisRuntime):
    token = _runtime.set(runtime)
    try:
        yield runtime
    finally:
        _runtime.reset(token)


async def resolve_tools(default_loader):
    runtime = current_runtime()
    if runtime.tools is not None:
        return list(runtime.tools)
    return await default_loader()


def create_model(**kwargs):
    runtime = current_runtime()
    if runtime.model_factory is not None:
        return runtime.model_factory(**kwargs)
    from app.core.llm_factory import llm_factory

    return llm_factory.create_chat_model(**kwargs)


async def get_mcp_client_with_retry(*args, **kwargs):
    from app.agent.mcp_client import get_mcp_client_with_retry as load

    return await load(*args, **kwargs)


async def load_mcp_tools_safe(*args, **kwargs):
    from app.agent.mcp_client import load_mcp_tools_safe as load

    return await load(*args, **kwargs)


class _LazyTools:
    def __iter__(self):
        from app.tools import DEFAULT_LOCAL_AGENT_TOOLS

        return iter(DEFAULT_LOCAL_AGENT_TOOLS)


class _LazyKnowledge:
    async def ainvoke(self, value):
        runtime = current_runtime()
        if runtime.knowledge_lookup is not None:
            return await runtime.knowledge_lookup(value["query"])
        if runtime.tools is not None:
            return ""  # Explicit isolated runtime, never consult a production KB.
        from app.tools import retrieve_knowledge

        return await retrieve_knowledge.ainvoke(value)


DEFAULT_LOCAL_AGENT_TOOLS = _LazyTools()
retrieve_knowledge = _LazyKnowledge()
