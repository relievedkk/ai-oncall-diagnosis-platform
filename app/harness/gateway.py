"""Credential-free execution sequencing for bounded remediation adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.config import config
from app.harness.contracts import (
    ActionIntent,
    ActionStatus,
    ExecutionResult,
    GatewayEvent,
    PreparedAction,
    VerificationResult,
)


def validate_remediation_adapter() -> None:
    if config.remediation_adapter != "dry_run":
        raise RuntimeError(
            "REMEDIATION_ADAPTER must be 'dry_run'; this release has no production adapter"
        )


class RemediationAdapter(Protocol):
    async def prepare(self, intent: ActionIntent) -> PreparedAction: ...

    async def execute(self, prepared: PreparedAction) -> ExecutionResult: ...

    async def observe(self, prepared: PreparedAction) -> dict[str, Any]: ...

    async def rollback(self, prepared: PreparedAction) -> ExecutionResult: ...


class DryRunRemediationAdapter:
    """Deterministic simulation that performs no infrastructure operation."""

    async def prepare(self, intent: ActionIntent) -> PreparedAction:
        payload = intent.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return PreparedAction(
            intent=intent,
            pre_change_snapshot={
                "dry_run": True,
                "target_service": intent.service_name,
            },
            verification_spec={
                "dry_run": True,
                "kind": intent.action_type.value,
            },
            rollback_token=f"dry-run:{digest}",
        )

    async def execute(self, prepared: PreparedAction) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            details={
                "dry_run": True,
                "simulated_action": prepared.intent.action_type.value,
                "target_service": prepared.intent.service_name,
            },
        )

    async def observe(self, prepared: PreparedAction) -> dict[str, Any]:
        return {
            "known": True,
            "success": True,
            "dry_run": True,
            "simulated_action": prepared.intent.action_type.value,
            "target_service": prepared.intent.service_name,
        }

    async def rollback(self, prepared: PreparedAction) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            details={
                "dry_run": True,
                "simulated_rollback": prepared.rollback_token,
            },
        )


class RemediationGateway:
    def __init__(
        self,
        adapter: RemediationAdapter | None = None,
        execution_timeout_seconds: float | None = None,
        verification_timeout_seconds: float | None = None,
    ) -> None:
        self.adapter = adapter or DryRunRemediationAdapter()
        self.execution_timeout_seconds = (
            execution_timeout_seconds or config.remediation_execution_timeout_seconds
        )
        self.verification_timeout_seconds = (
            verification_timeout_seconds or config.remediation_verification_timeout_seconds
        )

    async def run(self, intent: ActionIntent) -> AsyncIterator[GatewayEvent]:
        try:
            async with asyncio.timeout(self.execution_timeout_seconds):
                prepared = await self.adapter.prepare(intent)
        except Exception as exc:
            yield GatewayEvent(
                status=ActionStatus.FAILED,
                error=_error_message("prepare failed", exc),
            )
            return

        yield GatewayEvent(
            status=ActionStatus.EXECUTING,
            prepared_action=prepared,
        )

        try:
            async with asyncio.timeout(self.execution_timeout_seconds):
                execution = await self.adapter.execute(prepared)
        except TimeoutError:
            yield GatewayEvent(
                status=ActionStatus.RECONCILING,
                prepared_action=prepared,
            )
            observation = await self._observe(prepared)
            if observation is None or observation.get("known") is not True:
                yield GatewayEvent(
                    status=ActionStatus.FAILED,
                    prepared_action=prepared,
                    error="execution outcome remains unknown",
                )
                return
            execution = ExecutionResult(
                success=observation.get("success") is True,
                details={"reconciled": True, "observation": observation},
            )
        except Exception as exc:
            yield GatewayEvent(
                status=ActionStatus.FAILED,
                prepared_action=prepared,
                error=_error_message("execution failed", exc),
            )
            return
        else:
            if not execution.success:
                yield GatewayEvent(
                    status=ActionStatus.FAILED,
                    prepared_action=prepared,
                    result=execution,
                    error="adapter reported execution failure",
                )
                return
            observation = await self._observe(prepared)

        if observation is None or observation.get("known") is not True:
            yield GatewayEvent(
                status=ActionStatus.RECONCILING,
                prepared_action=prepared,
                result=execution,
            )
            yield GatewayEvent(
                status=ActionStatus.FAILED,
                prepared_action=prepared,
                result=execution,
                error="execution outcome remains unknown",
            )
            return

        async for event in self._verification_events(prepared, execution, observation):
            yield event

    async def reconcile(self, prepared: PreparedAction) -> AsyncIterator[GatewayEvent]:
        """Observe an uncertain prior attempt without executing it a second time."""
        observation = await self._observe(prepared)
        if observation is None or observation.get("known") is not True:
            yield GatewayEvent(
                status=ActionStatus.FAILED,
                prepared_action=prepared,
                error="execution outcome remains unknown",
            )
            return
        execution = ExecutionResult(
            success=observation.get("success") is True,
            details={"reconciled": True, "observation": observation},
        )
        async for event in self._verification_events(prepared, execution, observation):
            yield event

    async def _verification_events(
        self,
        prepared: PreparedAction,
        execution: ExecutionResult,
        observation: dict[str, Any],
    ) -> AsyncIterator[GatewayEvent]:
        verification = _verify(prepared, observation)
        yield GatewayEvent(
            status=ActionStatus.VERIFYING,
            prepared_action=prepared,
            result=execution,
            verification=verification,
        )
        if verification.success:
            yield GatewayEvent(
                status=ActionStatus.SUCCEEDED,
                prepared_action=prepared,
                result=execution,
                verification=verification,
            )
            return

        yield GatewayEvent(
            status=ActionStatus.ROLLING_BACK,
            prepared_action=prepared,
            result=execution,
            verification=verification,
        )
        try:
            async with asyncio.timeout(self.execution_timeout_seconds):
                rollback = await self.adapter.rollback(prepared)
        except Exception as exc:
            yield GatewayEvent(
                status=ActionStatus.FAILED,
                prepared_action=prepared,
                verification=verification,
                error=_error_message("rollback failed", exc),
            )
            return
        if rollback.success:
            yield GatewayEvent(
                status=ActionStatus.ROLLED_BACK,
                prepared_action=prepared,
                result=rollback,
                verification=verification,
            )
            return
        yield GatewayEvent(
            status=ActionStatus.FAILED,
            prepared_action=prepared,
            result=rollback,
            verification=verification,
            error="rollback failed",
        )

    async def _observe(self, prepared: PreparedAction) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout(self.verification_timeout_seconds):
                observation = await self.adapter.observe(prepared)
        except Exception:
            return None
        return observation if isinstance(observation, dict) else None


def _verify(
    prepared: PreparedAction,
    observation: dict[str, Any] | None,
) -> VerificationResult:
    if not observation or observation.get("known") is not True:
        return VerificationResult(
            success=False,
            observation=observation or {},
            reason="verification observation is missing or unknown",
        )

    spec = prepared.verification_spec
    if spec.get("dry_run") is True:
        success = (
            observation.get("dry_run") is True
            and observation.get("success") is True
        )
    elif spec.get("kind") == "restart_instance":
        success = (
            observation.get("replacement_observed") is True
            and observation.get("guards_passed") is True
        )
    elif spec.get("kind") == "scale_out":
        target = spec.get("target_replicas")
        success = (
            isinstance(target, int)
            and observation.get("desired_replicas") == target
            and isinstance(observation.get("available_replicas"), int)
            and observation["available_replicas"] >= target
            and observation.get("guards_passed") is True
        )
    elif spec.get("kind") == "rollback_revision":
        success = (
            observation.get("revision") == spec.get("target_revision")
            and observation.get("guards_passed") is True
        )
    else:
        success = observation.get("success") is True

    return VerificationResult(
        success=success,
        observation=observation,
        reason=None if success else "verification guards did not pass",
    )


def _error_message(prefix: str, error: Exception) -> str:
    detail = str(error).strip() or error.__class__.__name__
    return f"{prefix}: {detail}"[:512]
