"""Small durable-file helpers shared by standalone operational commands."""

import json
import os
import re
import tempfile
import time
from pathlib import Path


def atomic_json(path: Path, data: dict) -> None:
    """Publish complete JSON in one replace, keeping the prior file on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        retry_delays = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)
        for attempt in range(len(retry_delays) + 1):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == len(retry_delays):
                    raise
                time.sleep(retry_delays[attempt])
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_env(path: Path) -> dict[str, str]:
    """Read literal .env assignments without executing or expanding their contents."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError("Invalid environment assignment")
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise ValueError("Invalid quoted environment assignment")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key.strip()] = value
    return values
