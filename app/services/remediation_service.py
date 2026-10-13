"""Single authority boundary for remediation proposals and state changes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.config import config
from app.harness.contracts import (
    ActionIntent,
    ActionStatus,
    GatewayEvent,
    PolicyVerdict,
    RemediationMode,
)
from app.harness.policy import EvidenceSignal, PolicyContext, PolicyEngine
from app.repositories.remediation import RemediationRepository, remediation_repository
from app.services.service_catalog import ServiceCatalog, service_catalog

MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_SUMMARY = 256
LEGAL_GATEWAY_TRANSITIONS: dict[str, set[str]] = {
    ActionStatus.PROPOSED: {ActionStatus.EXECUTING},
    ActionStatus.APPROVED: {ActionStatus.EXECUTING},
    ActionStatus.EXECUTING: {
        ActionStatus.EXECUTING,
        ActionStatus.RECONCILING,
        ActionStatus.VERIFYING,
        ActionStatus.ROLLING_BACK,
        ActionStatus.FAILED,
    },
    ActionStatus.RECONCILING: {
        ActionStatus.VERIFYING,
        ActionStatus.ROLLING_BACK,
        ActionStatus.FAILED,
    },
    ActionStatus.VERIFYING: {
        ActionStatus.SUCCEEDED,
        ActionStatus.ROLLING_BACK,
        ActionStatus.FAILED,
    },
    ActionStatus.ROLLING_BACK: {
        ActionStatus.ROLLED_BACK,
        ActionStatus.FAILED,
    },
}
TERMINAL_GATEWAY_STATUSES = {
    ActionStatus.SUCCEEDED,
    ActionStatus.ROLLED_BACK,
    ActionStatus.FAILED,
}


class RemediationError(RuntimeError):
    pass


class RemediationNotFound(RemediationError):
    pass


class RemediationConflict(RemediationError):
    pass


class RemediationExpired(RemediationError):
    pass


class RemediationService:
    def __init__(
        self,
        repository: RemediationRepository = remediation_repository,
        catalog: ServiceCatalog = service_catalog,
        policy_engine: PolicyEngine | None = None,
        mode: RemediationMode | str | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.policy_engine = policy_engine or PolicyEngine()
        self.mode = RemediationMode(mode or config.remediation_mode)
        self._now = now_factory or (lambda: datetime.now(UTC))

    async def propose(
        self,
        diagnosis_id: str,
        intent: ActionIntent,
        evidence: list[dict[str, Any]],
        requested_by: str = "agent",
    ) -> dict[str, Any]:
        now = self._now()
        service_policy = self.catalog.remediation_policy(intent.service_name)
        snapshot = _freeze_evidence(evidence, now)
        service_active_count = await self.repository.count_active(intent.service_name)
        context = PolicyContext(
            mode=self.mode,
            service_policy=service_policy,
            evidence=tuple(
                EvidenceSignal(
                    evidence_type=item["evidence_type"],
                    success=item["success"],
                )
                for item in snapshot
                if item["evidence_type"] in {"alert", "metric", "log"}
            ),
            live_replica_count=_extract_live_replica_count(evidence),
            now=now,
            last_completed_action_at=await self.repository.last_completed_at(
                intent.service_name
            ),
            service_action_active=service_active_count > 0,
            global_active_count=await self.repository.count_active(),
            diagnosis_action_count=await self.repository.count_for_diagnosis(
                diagnosis_id
            ),
            global_active_limit=config.remediation_global_active_limit,
            max_actions_per_diagnosis=config.remediation_max_actions_per_diagnosis,
            default_cooldown_seconds=config.remediation_default_cooldown_seconds,
        )
        decision = self.policy_engine.evaluate(intent, context)

        status = ActionStatus.PROPOSED
        enqueue = decision.verdict is PolicyVerdict.ALLOW
        approval_expires_at = None
        completed_at = None
        if decision.verdict is PolicyVerdict.DENY:
            status = ActionStatus.DENIED
            completed_at = now
        elif decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
            status = ActionStatus.AWAITING_APPROVAL
            approval_expires_at = now + timedelta(
                seconds=config.remediation_approval_expiry_seconds
            )

        intent_payload = intent.model_dump(mode="json")
        intent_digest = hashlib.sha256(
            json.dumps(
                intent_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        values = {
            "id": str(uuid4()),
            "diagnosis_id": diagnosis_id,
            "idempotency_key": f"{diagnosis_id}:{intent_digest}",
            "action_type": intent.action_type.value,
            "target_service": intent.service_name,
            "risk_level": decision.risk_level.value,
            "status": status.value,
            "intent_payload": intent_payload,
            "evidence_snapshot": snapshot,
            "policy_decision": decision.verdict.value,
            "policy_reason": decision.reason_code,
            "requested_by": (requested_by or "agent")[:128],
            "approval_expires_at": approval_expires_at,
            "created_at": now,
            "updated_at": now,
            "completed_at": completed_at,
        }
        action, _created = await self.repository.create(values, enqueue=enqueue)
        return action

    async def decide(
        self,
        action_id: str,
        decision: Literal["approve", "reject"],
        principal_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        result = await self.repository.decide(
            action_id,
            decision,
            (principal_id or "unknown")[:128],
            now or self._now(),
        )
        if result == "missing":
            raise RemediationNotFound(action_id)
        if result == "expired":
            raise RemediationExpired(action_id)
        if result == "conflict":
            raise RemediationConflict(action_id)
        action = await self.repository.get(action_id)
        if action is None:
            raise RemediationNotFound(action_id)
        return action

    async def apply_gateway_event(
        self,
        action_id: str,
        event: GatewayEvent,
    ) -> dict[str, Any]:
        action = await self.repository.get(action_id)
        if action is None:
            raise RemediationNotFound(action_id)
        current = str(action["status"])
        target = event.status.value
        if target not in LEGAL_GATEWAY_TRANSITIONS.get(current, set()):
            raise RemediationConflict(f"illegal transition: {current} -> {target}")

        updates: dict[str, Any] = {"status": target, "updated_at": self._now()}
        if event.prepared_action is not None:
            updates["prepared_payload"] = event.prepared_action.model_dump(mode="json")
        if event.result is not None:
            updates["result_payload"] = event.result.model_dump(mode="json")
        if event.verification is not None:
            updates["verification_payload"] = event.verification.model_dump(mode="json")
        if event.error is not None:
            updates["error"] = event.error
        if event.status in TERMINAL_GATEWAY_STATUSES:
            updates["completed_at"] = self._now()

        changed = await self.repository.compare_and_set(
            action_id,
            {current},
            **updates,
        )
        if not changed:
            raise RemediationConflict(f"stale transition: {current} -> {target}")
        updated = await self.repository.get(action_id)
        if updated is None:
            raise RemediationNotFound(action_id)
        return updated

    async def get_action(self, action_id: str) -> dict[str, Any]:
        action = await self.repository.get(action_id)
        if action is None:
            raise RemediationNotFound(action_id)
        return action

    async def list_actions(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.repository.list(status=status, limit=limit)


def _freeze_evidence(
    evidence: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    snapshot = []
    for item in evidence[:MAX_EVIDENCE_ITEMS]:
        serialized = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        summary_source = item.get("summary", item.get("payload", ""))
        if isinstance(summary_source, str):
            summary = summary_source
        else:
            summary = json.dumps(
                summary_source,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        collected_at = item.get("collected_at", now.isoformat())
        if isinstance(collected_at, datetime):
            collected_at = collected_at.isoformat()
        snapshot.append(
            {
                "evidence_type": str(item.get("evidence_type", "unknown"))[:64],
                "source": str(item.get("source", "unknown"))[:128],
                "success": item.get("success") is True,
                "collected_at": str(collected_at)[:64],
                "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "summary": summary[:MAX_EVIDENCE_SUMMARY],
            }
        )
    return snapshot


def _extract_live_replica_count(evidence: list[dict[str, Any]]) -> int | None:
    for item in evidence:
        candidates = [item.get("live_replica_count")]
        payload = item.get("payload")
        if isinstance(payload, dict):
            candidates.extend(
                [payload.get("live_replica_count"), payload.get("current_replicas")]
            )
        for candidate in candidates:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    return None


remediation_service = RemediationService()
