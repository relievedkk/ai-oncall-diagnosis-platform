"""Single authority boundary for remediation proposals and state changes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.config import config
from app.core.evidence import decode_evidence_result, evidence_response_layers, valid_evidence
from app.core.remediation_evidence import evidence_authority_deadline
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
        *, execution_token: str | None = None,
    ) -> dict[str, Any]:
        async with self.repository.proposal_guard():
            now = self._now()
            await self.repository.expire_due_approvals(now)
            return await self._propose_under_guard(
                diagnosis_id,
                intent,
                evidence,
                requested_by,
                now,
                execution_token,
            )

    async def _propose_under_guard(
        self,
        diagnosis_id: str,
        intent: ActionIntent,
        evidence: list[dict[str, Any]],
        requested_by: str,
        now: datetime,
        execution_token: str | None = None,
    ) -> dict[str, Any]:
        service_policy = self.catalog.remediation_policy(intent.service_name)
        # Validate at the public authority boundary, even when the caller already
        # normalized evidence. Frozen audit records include rejected observations.
        evidence = evidence[:MAX_EVIDENCE_ITEMS]
        snapshot = _freeze_evidence(evidence, now, intent=intent, catalog=self.catalog)
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
            live_replica_count=_extract_live_replica_count(
                evidence, intent=intent, catalog=self.catalog, now=now
            ),
            now=now,
            last_completed_action_at=await self.repository.last_completed_at(intent.service_name),
            service_action_active=service_active_count > 0,
            global_active_count=await self.repository.count_active(),
            diagnosis_action_count=await self.repository.count_for_diagnosis(diagnosis_id),
            global_active_limit=config.remediation_global_active_limit,
            max_actions_per_diagnosis=config.remediation_max_actions_per_diagnosis,
            default_cooldown_seconds=config.remediation_default_cooldown_seconds,
            service_circuit_open=await self.repository.is_circuit_open(intent.service_name),
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
            approval_expires_at = min(
                now + timedelta(seconds=config.remediation_approval_expiry_seconds),
                evidence_authority_deadline(intent.model_dump(mode="json"), snapshot, now) or now,
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
        action, _created = await self.repository.create(values, enqueue=enqueue, execution_token=execution_token)
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
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        action = await self.repository.get(action_id)
        if action is None:
            raise RemediationNotFound(action_id)
        current = str(action["status"])
        if claim_token is not None and action.get("worker_name") != claim_token:
            raise RemediationConflict("execution claim is no longer owned by this worker")
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
        if event.status is ActionStatus.FAILED and (
            current == ActionStatus.ROLLING_BACK or event.requires_manual_review
        ):
            updates["circuit_breaker_open"] = True
            updates["circuit_opened_at"] = self._now()
        if event.status in TERMINAL_GATEWAY_STATUSES:
            updates["completed_at"] = self._now()

        changed = await self.repository.compare_and_set(
            action_id,
            {current},
            expected_worker_name=claim_token,
            **updates,
        )
        if not changed:
            raise RemediationConflict(f"stale transition: {current} -> {target}")
        updated = await self.repository.get(action_id)
        if updated is None:
            raise RemediationNotFound(action_id)
        return updated

    async def disable_before_execution(self, action_id: str) -> bool:
        now = self._now()
        return await self.repository.compare_and_set(
            action_id,
            {ActionStatus.PROPOSED.value, ActionStatus.APPROVED.value},
            status=ActionStatus.DENIED.value,
            completed_at=now,
            updated_at=now,
            error="remediation mode is off",
        )

    async def get_action(self, action_id: str) -> dict[str, Any]:
        await self.repository.expire_due_approvals(self._now())
        action = await self.repository.get(action_id)
        if action is None:
            raise RemediationNotFound(action_id)
        return action

    async def list_actions(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.repository.expire_due_approvals(self._now())
        return await self.repository.list(status=status, limit=limit)


def _freeze_evidence(
    evidence: list[dict[str, Any]],
    now: datetime,
    *,
    intent: ActionIntent,
    catalog: ServiceCatalog,
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
        valid, reason, valid_until = _validate_authorizing_evidence(item, intent, catalog, now)
        collected_at = item.get("collected_at", "")
        if isinstance(collected_at, datetime):
            collected_at = collected_at.isoformat()
        snapshot.append(
            {
                "evidence_type": str(item.get("evidence_type", "unknown"))[:64],
                "source": str(item.get("source", "unknown"))[:128],
                "success": valid,
                "collected_at": str(collected_at)[:64],
                "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "summary": summary[:MAX_EVIDENCE_SUMMARY],
                "validation_reason": reason,
                "validated_at": now.isoformat(),
                "valid_until": valid_until.isoformat() if valid_until else None,
                "target_service": intent.service_name,
                "target_instance": getattr(intent, "instance_id", None),
            }
        )
    return snapshot


def _parse_evidence_time(value: Any, *, divisor: int = 1) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            return datetime.fromtimestamp(value / divisor, UTC)
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _target_matches(
    layers: list[dict[str, Any]], intent: ActionIntent, catalog: ServiceCatalog
) -> bool:
    services = []
    instances = []
    for layer in layers:
        contexts = [layer]
        for key in ("query", "labels"):
            if isinstance(layer.get(key), dict):
                contexts.append(layer[key])
        for context in contexts:
            services.extend(context[key] for key in ("service_name", "service", "job", "app") if key in context)
            instances.extend(context[key] for key in ("instance_id", "instance") if key in context)
    if not services or any(
        not isinstance(value, str)
        or not value.strip()
        or catalog.resolve_name(value)[0] != intent.service_name
        for value in services
    ):
        return False
    target_instance = getattr(intent, "instance_id", None)
    if target_instance is not None:
        return bool(instances) and all(value == target_instance for value in instances)
    # Service-level actions may use multiple instances; conflicting service
    # identities remain forbidden. No instance is inferred from a service name.
    return True


def _validate_authorizing_evidence(
    item: dict[str, Any], intent: ActionIntent, catalog: ServiceCatalog, now: datetime
) -> tuple[bool, str, datetime | None]:
    layers = evidence_response_layers(item)
    if not layers or layers[-1].get("success") is not True or not valid_evidence(item):
        return False, "invalid_observation", None
    if now.tzinfo is None:
        return False, "invalid_validation_time", None
    max_age = timedelta(seconds=config.remediation_evidence_max_age_seconds)
    skew = timedelta(seconds=config.remediation_evidence_clock_skew_seconds)
    collected = _parse_evidence_time(item.get("collected_at"))
    if collected is None:
        return False, "missing_collection_time", None
    collection_times = [collected]
    for layer in layers:
        if "collected_at" in layer:
            timestamp = _parse_evidence_time(layer["collected_at"])
            if timestamp is None:
                return False, "invalid_collection_time", None
            collection_times.append(timestamp)
    if any(timestamp > now + skew or now - timestamp > max_age for timestamp in collection_times):
        return False, "expired_or_future_collection", None
    payload = layers[-1]
    kind = item.get("evidence_type")
    observed_times = []
    if kind == "metric":
        points = payload["data_points"]
        if not _target_matches(layers + points, intent, catalog):
            return False, "target_mismatch", None
        observed_times = [_parse_evidence_time(point["timestamp"]) for point in points]
    elif kind == "alert":
        matching = [alert for alert in payload["alerts"] if _target_matches(layers + [alert], intent, catalog)]
        if not any(alert.get("state", alert.get("status")) == "firing" for alert in matching):
            return False, "missing_target_firing_alert", None
        # active_at is the start of a potentially long-running firing alert;
        # freshness comes from this successful current alert query's collection.
    elif kind == "log":
        for record in payload.get("records", payload.get("logs", [])):
            raw = record.get("LogJson", record)
            raw = json.loads(raw) if isinstance(raw, str) else raw
            if not _target_matches(layers + [record, raw], intent, catalog):
                continue
            if "timestamp_us" in raw:
                observed_times.append(_parse_evidence_time(raw["timestamp_us"], divisor=1_000_000))
            elif "Time" in record:
                observed_times.append(_parse_evidence_time(record["Time"], divisor=1_000))
            else:
                observed_times.append(_parse_evidence_time(raw.get("timestamp")))
        if not observed_times:
            return False, "missing_target_log", None
    if observed_times:
        if any(timestamp is None or timestamp > now + skew for timestamp in observed_times):
            return False, "invalid_or_future_observation", None
        latest = max(observed_times)
        if now - latest > max_age:
            return False, "expired_observation", None
        collection_times.append(latest)
    return True, "valid", min(collection_times) + max_age


def _extract_live_replica_count(
    evidence: list[dict[str, Any]], *, intent: ActionIntent | None = None,
    catalog: ServiceCatalog | None = None, now: datetime | None = None,
) -> int | None:
    """Use only fresh, successful target metrics; conflicting counts deny scale."""
    if intent is None or catalog is None or now is None:
        return None
    counts = set()
    for item in evidence[:MAX_EVIDENCE_ITEMS]:
        if item.get("evidence_type") != "metric":
            continue
        if not _validate_authorizing_evidence(item, intent, catalog, now)[0]:
            continue
        payload = decode_evidence_result(item)
        for key in ("live_replica_count", "current_replicas"):
            if key in payload:
                candidate = payload[key]
                if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
                    return None
                counts.add(candidate)
    return next(iter(counts)) if len(counts) == 1 else None


remediation_service = RemediationService()
