"""Isolated Celery task for idempotent remediation execution."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from celery import Task

from app.config import config
from app.core.metrics import REMEDIATION_ACTION_EVENTS, REMEDIATION_IN_FLIGHT
from app.harness.contracts import (
    ExecutionResult,
    GatewayEvent,
    PreparedAction,
    RemediationMode,
    parse_action_intent,
)
from app.harness.gateway import RemediationGateway, validate_remediation_adapter
from app.remediation_celery_app import celery_app
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
    validate_remediation_adapter()
    action = await service.get_action(action_id)
    if RemediationMode(config.remediation_mode) is RemediationMode.OFF and action["status"] in {
        "proposed",
        "approved",
    }:
        disabled = await service.disable_before_execution(action_id)
        return "disabled" if disabled else "ignored"

    claim_token = f"{worker_name[:218]}:{uuid4().hex}"
    claim = await service.repository.claim_execution(
        action_id,
        claim_token,
        lease_seconds=config.remediation_worker_lease_seconds,
    )
    if claim in {"busy", "ignored"}:
        return claim
    if claim == "failed":
        return "failed"

    action = await service.get_action(action_id)
    try:
        intent = parse_action_intent(action["intent_payload"])
        if claim == "reconcile":
            prepared = PreparedAction.model_validate(action["prepared_payload"])
            if prepared.intent != intent:
                raise ValueError("prepared action does not match the authorized intent")
            execution = (
                ExecutionResult.model_validate(action["result_payload"])
                if action.get("result_payload") is not None
                else None
            )
            events = gateway.reconcile(prepared, execution=execution)
        else:
            events = gateway.run(intent)
    except (ValueError, TypeError, KeyError):
        await service.apply_gateway_event(
            action_id,
            GatewayEvent(
                status="failed",
                error="invalid durable action payload; manual review required",
                requires_manual_review=claim == "reconcile",
            ),
            claim_token=claim_token,
        )
        return "failed"

    if claim not in {"execute", "reconcile"}:
        return "ignored"
    action_type = intent.action_type.value
    REMEDIATION_IN_FLIGHT.labels(action_type=action_type).inc()
    final_status = "executing"
    try:
        async for event in events:
            await service.apply_gateway_event(action_id, event, claim_token=claim_token)
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
    try:
        result = _run_async(execute_remediation(action_id, worker_name))
    except Exception as exc:
        raise self.retry(
            exc=exc,
            countdown=min(config.remediation_worker_lease_seconds, 300),
        ) from exc
    if result == "busy":
        raise self.retry(countdown=config.remediation_worker_lease_seconds)
    return result
