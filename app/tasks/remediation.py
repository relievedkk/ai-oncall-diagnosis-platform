"""Isolated Celery task for idempotent remediation execution."""

from __future__ import annotations

import asyncio

from celery import Task

from app.celery_app import celery_app
from app.core.metrics import REMEDIATION_ACTION_EVENTS, REMEDIATION_IN_FLIGHT
from app.harness.contracts import parse_action_intent
from app.harness.gateway import RemediationGateway
from app.services.remediation_service import RemediationService, remediation_service

_worker_loop: asyncio.AbstractEventLoop | None = None
_gateway = RemediationGateway()


async def execute_remediation(
    action_id: str,
    worker_name: str,
    *,
    service: RemediationService = remediation_service,
    gateway: RemediationGateway = _gateway,
) -> str:
    """Claim once, apply every gateway event in order, and return final status."""
    claimed = await service.repository.claim_execution(action_id, worker_name)
    if not claimed:
        return "ignored"

    action = await service.get_action(action_id)
    intent = parse_action_intent(action["intent_payload"])
    action_type = intent.action_type.value
    REMEDIATION_IN_FLIGHT.labels(action_type=action_type).inc()
    final_status = "executing"
    try:
        async for event in gateway.run(intent):
            await service.apply_gateway_event(action_id, event)
            final_status = event.status.value
            REMEDIATION_ACTION_EVENTS.labels(
                action_type=action_type,
                status=final_status,
            ).inc()
        return final_status
    finally:
        REMEDIATION_IN_FLIGHT.labels(action_type=action_type).dec()


def _run_async(coroutine):
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    return _worker_loop.run_until_complete(coroutine)


@celery_app.task(
    bind=True,
    name="app.tasks.remediation.run_remediation",
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_remediation(self: Task, action_id: str) -> str:
    worker_name = str(self.request.hostname or "remediation-worker")
    return _run_async(execute_remediation(action_id, worker_name))
