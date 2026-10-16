"""Remove configured secrets and common credential forms from log output."""

import re
import traceback
from collections.abc import Callable, Iterable
from typing import Any

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_CREDENTIAL = re.compile(
    r"""(?i)(["']?(?:password|secret[_-]?key|secret[_-]?id|api[_-]?key|access[_-]?token|token|authorization)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)"""
)
_URL_PASSWORD = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^:/\s]+:)[^@\s]+@")


def make_redactor(secrets: Iterable[str]) -> Callable[[str], str]:
    known = sorted(
        {value for value in secrets if isinstance(value, str) and len(value) >= 8},
        key=len,
        reverse=True,
    )

    def redact(text: str) -> str:
        for value in known:
            text = text.replace(value, "[REDACTED]")
        text = _BEARER.sub("Bearer [REDACTED]", text)
        text = _URL_PASSWORD.sub(r"\1[REDACTED]@", text)
        return _CREDENTIAL.sub(r"\1[REDACTED]", text)

    return redact


def patch_record(record: dict[str, Any], redact: Callable[[str], str]) -> None:
    text = record["message"]
    exception = record.get("exception")
    if exception:
        # Standard traceback preserves useful frames without expanding locals.
        text += "\n" + "".join(
            traceback.format_exception(exception.type, exception.value, exception.traceback)
        )
        record["exception"] = None
    record["message"] = redact(text)
