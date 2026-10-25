"""Bounded standalone HTTP checks with hysteresis and local transition events."""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import atomic_json, read_env


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward machine credentials to a redirected host.
        return None


def _check(check, timeout, environment):
    started = time.monotonic()
    result = {"ok": False, "error": None}
    try:
        url = check["url"]
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            result["error"] = "invalid_url"
            return result
        headers = {}
        key_name = check.get("api_key_env")
        if key_name:
            key = environment.get(key_name)
            if not key:
                result["error"] = "missing_credential"
                return result
            headers[check.get("header", "X-API-Key")] = check.get("prefix", "") + key
        request = Request(url, headers=headers, method="GET")
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            result["status_code"] = response.status
            result["ok"] = 200 <= response.status < 300
            if not result["ok"]:
                result["error"] = f"http_{response.status}"
    except HTTPError as error:
        result["status_code"] = error.code
        result["error"] = f"http_{error.code}"
        error.close()
    except TimeoutError:
        result["error"] = "timeout"
    except URLError as error:
        result["error"] = "timeout" if isinstance(error.reason, TimeoutError) else "connection_failed"
    except (OSError, ValueError, KeyError, TypeError):
        result["error"] = "invalid_check"
    finally:
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def run_once(config: dict, state_dir: Path) -> dict:
    """Return immediate check health and durable debounced transition events.

    Config: ``checks=[{name,url,api_key_env?,header?,prefix?,timeout?}]``,
    ``timeout=2`` (0 < timeout <= 30), optional ``env_file``. Environment
    overrides literal file values. URLs and exception messages are never stored.
    Exit code 0 means every immediate check succeeded, 1 means degraded.
    """
    checks = config.get("checks", [])
    if not isinstance(checks, list) or not checks:
        raise ValueError("At least one probe check is required")
    names = [check.get("name", "") for check in checks]
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names) or len(set(names)) != len(names):
        raise ValueError("Probe check names must be unique safe identifiers")
    timeout = float(config.get("timeout", 2))
    timeouts = [float(check.get("timeout", timeout)) for check in checks]
    if not 0 < timeout <= 30 or any(not 0 < item <= 30 for item in timeouts):
        raise ValueError("Probe timeout must be positive and at most 30 seconds")
    environment = read_env(Path(config["env_file"])) if config.get("env_file") else {}
    environment.update(os.environ)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads((state_dir / "probe.json").read_text(encoding="utf-8"))
        previous_checks = previous.get("checks", {})
        if not isinstance(previous_checks, dict):
            previous_checks = {}
    except (OSError, ValueError, AttributeError):
        previous_checks = {}
    checked_at = datetime.now(UTC).isoformat()
    results, events = {}, []
    for check, check_timeout in zip(checks, timeouts, strict=True):
        name = check["name"]
        result = _check(check, check_timeout, environment)
        old = previous_checks.get(name, {})
        status = old.get("status", "healthy")
        failures = 0 if result["ok"] else old.get("consecutive_failures", 0) + 1
        successes = old.get("consecutive_successes", 0) + 1 if result["ok"] else 0
        next_status = status
        if failures >= 3:
            next_status = "degraded"
        elif successes >= 2:
            next_status = "healthy"
        if next_status != status:
            events.append({"check": name, "status": next_status, "at": checked_at})
        result.update(status=next_status, consecutive_failures=failures, consecutive_successes=successes)
        results[name] = result
    healthy = all(result["ok"] for result in results.values())
    result = {
        "last_run": checked_at, "healthy": healthy, "degraded": not healthy,
        "checks": results, "events": events, "exit_code": 0 if healthy else 1,
    }
    # One writer per probe state directory; publish the complete state atomically.
    atomic_json(state_dir / "probe.json", result)
    if events:
        with (state_dir / "events.jsonl").open("a", encoding="utf-8") as output:
            for event in events:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
    return result
