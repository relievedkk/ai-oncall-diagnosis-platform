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
DIAGNOSIS_JOBS = Gauge(
    "oncall_diagnosis_jobs",
    "Persisted diagnosis jobs by current status.",
    ("status",),
)
OUTBOX_EVENTS = Gauge(
    "oncall_outbox_events",
    "Transactional outbox events by current status.",
    ("status",),
)
OUTBOX_OLDEST_PENDING_SECONDS = Gauge(
    "oncall_outbox_oldest_pending_seconds",
    "Age in seconds of the oldest unpublished diagnosis event.",
)
RABBITMQ_QUEUE_MESSAGES = Gauge(
    "oncall_rabbitmq_queue_messages",
    "Messages ready or unacknowledged in a diagnosis queue.",
    ("queue", "state"),
)
RABBITMQ_QUEUE_CONSUMERS = Gauge(
    "oncall_rabbitmq_queue_consumers",
    "Consumers attached to a diagnosis queue.",
    ("queue",),
)
QUEUE_OBSERVER_UP = Gauge(
    "oncall_queue_observer_up",
    "Whether the latest authenticated RabbitMQ management scrape succeeded.",
)
REMEDIATION_ACTION_EVENTS = Counter(
    "oncall_remediation_action_events",
    "Remediation gateway events by bounded action type and status.",
    ("action_type", "status"),
)
REMEDIATION_IN_FLIGHT = Gauge(
    "oncall_remediation_in_flight",
    "Remediation actions currently owned by a worker.",
    ("action_type",),
)
REMEDIATION_ACTIONS = Gauge(
    "oncall_remediation_actions",
    "Persisted remediation actions by bounded status.",
    ("status",),
)
REMEDIATION_OLDEST_AWAITING_APPROVAL_SECONDS = Gauge(
    "oncall_remediation_oldest_awaiting_approval_seconds",
    "Age in seconds of the oldest remediation awaiting human approval.",
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
