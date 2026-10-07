"""Application metrics used by Prometheus alerts and AIOps diagnostics."""

from __future__ import annotations

import time

import psutil
from fastapi import Request
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "oncall_http_requests",
    "HTTP requests handled by the application.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "oncall_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
AIOPS_DIAGNOSES = Counter(
    "oncall_aiops_diagnoses",
    "AIOps diagnosis runs by trigger and terminal status.",
    ("trigger", "status"),
)
AIOPS_ACTIVE = Gauge(
    "oncall_aiops_active_diagnoses",
    "Number of AIOps diagnoses currently running.",
    ("trigger",),
)
SYNTHETIC_FAULT = Gauge(
    "oncall_synthetic_fault",
    "Controlled fault used only by the local end-to-end test harness.",
    ("service", "fault"),
)
PROCESS_CPU_PERCENT = Gauge(
    "oncall_process_cpu_percent",
    "Current FastAPI process CPU usage as a percentage of one logical CPU.",
)
PROCESS_MEMORY_BYTES = Gauge(
    "oncall_process_resident_memory_bytes",
    "Current FastAPI process resident memory in bytes.",
)
_PROCESS = psutil.Process()


def refresh_process_metrics() -> None:
    """Refresh cross-platform process metrics immediately before a scrape."""
    PROCESS_CPU_PERCENT.set(_PROCESS.cpu_percent(interval=None))
    PROCESS_MEMORY_BYTES.set(_PROCESS.memory_info().rss)


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return str(route_path or "unmatched")


async def prometheus_http_middleware(request: Request, call_next):
    """Record low-cardinality request count and latency metrics."""
    if request.url.path == "/metrics":
        return await call_next(request)

    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        method = request.method
        route = _route_label(request)
        HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
        HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(
            time.perf_counter() - started
        )
