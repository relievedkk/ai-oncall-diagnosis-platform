"""Prometheus-backed monitoring MCP server.

Unlike the original demo implementation, every returned data point comes from
Prometheus's HTTP API and includes the executed query and time range.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastmcp import FastMCP

from app.config import config
from app.services.service_catalog import service_catalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("Monitor_MCP_Server")
mcp = FastMCP("Monitor")

_SAFE_SERVICE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SUPPORTED_METRICS = {"cpu", "memory", "error_rate", "latency_p95", "up"}


def log_tool_call(func: Callable[..., dict[str, Any]]):
    """Log tool calls without logging credentials or unbounded payloads."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info("Calling %s with %s", func.__name__, json.dumps(kwargs, ensure_ascii=False))
        try:
            result = func(*args, **kwargs)
            logger.info(
                "%s completed: success=%s points=%s",
                func.__name__,
                result.get("success"),
                len(result.get("data_points", [])),
            )
            return result
        except Exception:
            logger.exception("%s failed", func.__name__)
            raise

    return wrapper


def parse_time_or_default(time_str: str | None, default_offset_hours: int = 0) -> datetime:
    if time_str:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(time_str, fmt).astimezone()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time must be YYYY-MM-DD HH:MM:SS or ISO-8601") from exc
    return datetime.now().astimezone() + timedelta(hours=default_offset_hours)


def _step_seconds(interval: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", interval)
    if not match:
        raise ValueError("interval must use the form 30s, 1m, 5m, or 1h")
    value = int(match.group(1))
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return max(15, value * multiplier)


def _escape_label(value: str) -> str:
    if not _SAFE_SERVICE.fullmatch(value):
        raise ValueError("service_name contains unsupported characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _metric_query(service_name: str, metric: str) -> tuple[str, str, str]:
    canonical_name, service = service_catalog.resolve_name(service_name)
    job = str(service.get("prometheus_job") or canonical_name)
    escaped_job = _escape_label(job)
    configured = service.get("metrics", {}).get(metric)
    if configured:
        query = str(configured)
    else:
        defaults = {
            "cpu": f'sum(oncall_process_cpu_percent{{job="{escaped_job}"}})',
            "memory": f'sum(oncall_process_resident_memory_bytes{{job="{escaped_job}"}})',
            "error_rate": (
                f'sum(rate(oncall_http_requests_total{{job="{escaped_job}",status=~"5.."}}[5m])) '
                f'/ clamp_min(sum(rate(oncall_http_requests_total{{job="{escaped_job}"}}[5m])), 0.001)'
            ),
            "latency_p95": (
                "histogram_quantile(0.95, sum by (le) "
                f'(rate(oncall_http_request_duration_seconds_bucket{{job="{escaped_job}"}}[5m])))'
            ),
            "up": f'up{{job="{escaped_job}"}}',
        }
        query = defaults[metric]
    unit = {
        "cpu": "percent_of_one_cpu_core",
        "memory": "bytes",
        "error_rate": "ratio",
        "latency_p95": "seconds",
        "up": "boolean",
    }[metric]
    return canonical_name, query, unit


def _query_range(query: str, start: datetime, end: datetime, step: int) -> list[dict[str, Any]]:
    if end <= start:
        raise ValueError("end_time must be later than start_time")
    url = f"{config.prometheus_base_url.rstrip('/')}/api/v1/query_range"
    with httpx.Client(timeout=config.prometheus_request_timeout, trust_env=False) as client:
        response = client.get(
            url,
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(str(payload.get("error") or "Prometheus returned non-success status"))

    aggregate: dict[float, float] = {}
    for series in payload.get("data", {}).get("result", []):
        for timestamp, raw_value in series.get("values", []):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                aggregate[float(timestamp)] = aggregate.get(float(timestamp), 0.0) + value

    return [
        {
            "timestamp": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
            "value": round(value, 6),
        }
        for timestamp, value in sorted(aggregate.items())
    ]


def _query_service_metric(
    service_name: str,
    metric: str,
    start_time: str | None,
    end_time: str | None,
    interval: str,
) -> dict[str, Any]:
    if metric not in _SUPPORTED_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(sorted(_SUPPORTED_METRICS))}")
    start = parse_time_or_default(start_time, default_offset_hours=-1)
    end = parse_time_or_default(end_time, default_offset_hours=0)
    step = _step_seconds(interval)
    canonical_name, query, unit = _metric_query(service_name, metric)

    try:
        data_points = _query_range(query, start, end, step)
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        return {
            "success": False,
            "source": "prometheus",
            "service_name": canonical_name,
            "metric": metric,
            "promql": query,
            "time_range": {"start": start.isoformat(), "end": end.isoformat(), "step_seconds": step},
            "data_points": [],
            "error": str(exc),
        }

    values = [float(point["value"]) for point in data_points]
    statistics = {}
    if values:
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
        statistics = {
            "avg": round(sum(values) / len(values), 6),
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "p95": round(ordered[p95_index], 6),
        }

    return {
        "success": True,
        "source": "prometheus",
        "service_name": canonical_name,
        "metric": metric,
        "unit": unit,
        "promql": query,
        "time_range": {"start": start.isoformat(), "end": end.isoformat(), "step_seconds": step},
        "data_points": data_points,
        "statistics": statistics,
        "message": f"Prometheus returned {len(data_points)} real data points",
    }


@mcp.tool()
@log_tool_call
def query_service_metric(
    service_name: str,
    metric: str,
    start_time: str | None = None,
    end_time: str | None = None,
    interval: str = "1m",
) -> dict[str, Any]:
    """Query a real Prometheus metric for a service.

    metric supports cpu, memory, error_rate, latency_p95, and up. The response
    includes the exact PromQL and time range used as diagnosis evidence.
    """
    return _query_service_metric(service_name, metric, start_time, end_time, interval)


@mcp.tool()
@log_tool_call
def query_cpu_metrics(
    service_name: str,
    start_time: str | None = None,
    end_time: str | None = None,
    interval: str = "1m",
) -> dict[str, Any]:
    """Query real process CPU usage from Prometheus for the specified service."""
    return _query_service_metric(service_name, "cpu", start_time, end_time, interval)


@mcp.tool()
@log_tool_call
def query_memory_metrics(
    service_name: str,
    start_time: str | None = None,
    end_time: str | None = None,
    interval: str = "1m",
) -> dict[str, Any]:
    """Query real resident memory bytes from Prometheus for the specified service."""
    return _query_service_metric(service_name, "memory", start_time, end_time, interval)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8004, path="/mcp")
