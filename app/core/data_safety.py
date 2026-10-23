"""Deterministic model/tool data limits and durable diagnosis budget context."""

import json
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.config import config
from oncall_logging.redaction import make_redactor

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|apikey|accesstoken|refreshtoken|authorization|cookie|privatekey|sessiontoken)",
    re.I,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.S | re.I,
)
_COOKIE = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+")


class ModelInputRejected(ValueError):
    """A provider call would exceed the declared input boundary."""


class ModelBudgetExceeded(RuntimeError):
    """The persisted diagnosis model budget is exhausted."""


@lru_cache(maxsize=1)
def _text_redactor():
    known = [
        value
        for name in type(config).model_fields
        if _secret_key(name)
        and isinstance(value := getattr(config, name), str)
        and len(value) >= 8
    ]
    return make_redactor(known)


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", key).casefold())
    return bool(_SECRET_KEY.search(normalized)) or normalized.endswith("token")


def redact_sensitive(value: Any, *, _depth: int = 0) -> Any:
    """Preserve facts and schema while removing credential-shaped data."""
    if _depth >= 16:
        return "[TRUNCATED: nesting limit]"
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if _secret_key(str(key))
            else redact_sensitive(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_sensitive(item, _depth=_depth + 1) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            redacted = redact_sensitive(parsed, _depth=_depth + 1)
            if redacted != parsed:
                return json.dumps(redacted, ensure_ascii=False)
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _COOKIE.sub(lambda match: match[1] + REDACTED, value)
    return _text_redactor()(value)


def json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def model_input_bytes(parameters: dict) -> int:
    """Count schemas as the SDK sends them while retaining runtime parser types."""
    from openai.lib._parsing import type_to_response_format_param

    # LangChain tracing/parser hints are not provider request parameters.
    counted = {
        key: value
        for key, value in parameters.items()
        if key not in {"_type", "structured_output_format", "ls_structured_output_format"}
    }
    if isinstance(counted.get("response_format"), type):
        counted["response_format"] = type_to_response_format_param(counted["response_format"])
    return json_bytes(counted)


def bounded_tool_result(value: Any, *, max_bytes: int | None = None) -> Any:
    """Return usable small evidence or an explicit, non-authorizing preview."""
    limit = config.tool_result_max_bytes if max_bytes is None else max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 512:
        raise ValueError("Tool result budget must be an integer of at least 512 bytes")
    truncated = False

    def bounded(item, depth=0):
        nonlocal truncated
        if depth >= 12:
            truncated = True
            return "[TRUNCATED: nesting limit]"
        if isinstance(item, str):
            # Parse/redact the complete value: cutting JSON or a PEM block first
            # destroys the structure needed to recognize credentials.
            item = redact_sensitive(item)
            if len(item) > limit:
                truncated = True
                item = item[:limit]
            return item
        if isinstance(item, dict):
            if len(item) > 128:
                truncated = True
            return {
                str(key): REDACTED if _secret_key(str(key)) else bounded(child, depth + 1)
                for key, child in list(item.items())[:128]
            }
        if isinstance(item, list | tuple):
            if len(item) > 128:
                truncated = True
            return [bounded(child, depth + 1) for child in item[:128]]
        return item

    result = bounded(value)
    if not truncated and json_bytes(result) <= limit:
        return result
    preview = json.dumps(result, ensure_ascii=False, default=str)[: limit // 2]
    envelope = {
        "success": False,
        "truncated": True,
        "error": "tool_result_truncated",
        "message": "工具结果超过安全上限，请缩小查询范围；预览不能作为完整授权证据",
        "preview": preview,
    }
    while json_bytes(envelope) > limit:
        envelope["preview"] = envelope["preview"][: len(envelope["preview"]) // 2]
    return envelope


def sanitize_tool_message(message):
    """Bound results before LangGraph can checkpoint, stream, or reuse them."""
    from langchain_core.messages import ToolMessage

    if not isinstance(message, ToolMessage):
        return message
    content = bounded_tool_result(message.content)
    if not isinstance(content, str | list):
        content = json.dumps(content, ensure_ascii=False, default=str)
    # Artifacts are not needed by our tools and must not retain a raw side copy.
    return message.model_copy(update={"content": content, "artifact": None})


@dataclass(frozen=True)
class _BudgetOwner:
    repository: Any
    diagnosis_id: str
    execution_token: str


_budget_owner: ContextVar[_BudgetOwner | None] = ContextVar("diagnosis_model_budget", default=None)


@contextmanager
def diagnosis_budget_scope(repository, diagnosis_id: str, execution_token: str):
    reset = _budget_owner.set(_BudgetOwner(repository, diagnosis_id, execution_token))
    try:
        yield
    finally:
        _budget_owner.reset(reset)


async def reserve_model_input(messages, invocation_params=None) -> None:
    data = {
        **(invocation_params or {}),
        "messages": [
            {
                "role": message.type,
                "content": redact_sensitive(message.content),
                "tool_calls": redact_sensitive(getattr(message, "tool_calls", [])),
            }
            for batch in messages
            for message in batch
        ],
    }
    size = model_input_bytes(data)
    if size > config.model_max_input_bytes:
        raise ModelInputRejected("模型输入超过安全上限，请缩小问题或证据范围")
    owner = _budget_owner.get()
    if owner is not None:
        allowed = await owner.repository.reserve_model_budget(
            owner.diagnosis_id,
            execution_token=owner.execution_token,
            input_bytes=size,
            max_calls=config.diagnosis_max_model_calls,
            max_input_bytes=config.diagnosis_max_model_input_bytes,
        )
        if not allowed:
            raise ModelBudgetExceeded("诊断模型调用或输入预算已耗尽，停止调用模型")
