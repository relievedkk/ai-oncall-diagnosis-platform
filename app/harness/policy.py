"""Pure deterministic policy evaluation for typed remediation intents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.harness.contracts import (
    ActionIntent,
    ActionType,
    PolicyDecision,
    PolicyVerdict,
    RemediationMode,
    RestartInstanceIntent,
    RiskLevel,
    RollbackRevisionIntent,
    ScaleOutIntent,
)


@dataclass(frozen=True, slots=True)
class EvidenceSignal:
    evidence_type: Literal["alert", "metric", "log"]
    success: bool


@dataclass(frozen=True, slots=True)
class PolicyContext:
    mode: RemediationMode
    service_policy: Mapping[str, Any]
    evidence: tuple[EvidenceSignal, ...]
    live_replica_count: int | None
    now: datetime
    last_completed_action_at: datetime | None
    service_action_active: bool
    global_active_count: int
    diagnosis_action_count: int
    global_active_limit: int = 5
    max_actions_per_diagnosis: int = 2
    default_cooldown_seconds: int = 900


class PolicyEngine:
    """Evaluate authority without calling a model, database, or adapter."""

    def evaluate(
        self,
        intent: ActionIntent,
        context: PolicyContext,
    ) -> PolicyDecision:
        risk = _risk_for(intent)

        if context.mode is RemediationMode.OFF:
            return _deny(risk, "mode_off")

        policy = context.service_policy
        if policy.get("enabled") is not True:
            return _deny(risk, "service_disabled")

        allowed_modes = policy.get("allowed_modes")
        if allowed_modes is not None:
            if not isinstance(allowed_modes, list) or context.mode.value not in allowed_modes:
                return _deny(risk, "service_disabled")

        allowed_actions = policy.get("allowed_actions")
        if not isinstance(allowed_actions, list) or intent.action_type.value not in allowed_actions:
            return _deny(risk, "action_not_allowed")

        if context.service_action_active:
            return _deny(risk, "service_busy")
        if (
            context.global_active_count < 0
            or context.diagnosis_action_count < 0
            or context.global_active_limit < 1
            or context.max_actions_per_diagnosis < 1
            or context.global_active_count >= context.global_active_limit
            or context.diagnosis_action_count >= context.max_actions_per_diagnosis
        ):
            return _deny(risk, "budget_exhausted")

        cooldown = policy.get("cooldown_seconds", context.default_cooldown_seconds)
        if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
            return _deny(risk, "missing_target_state")
        if context.last_completed_action_at is not None:
            try:
                elapsed = (context.now - context.last_completed_action_at).total_seconds()
            except (TypeError, ValueError):
                return _deny(risk, "missing_target_state")
            if elapsed < 0:
                return _deny(risk, "missing_target_state")
            if elapsed < cooldown:
                return _deny(risk, "cooldown_active")

        successful_evidence = {
            signal.evidence_type for signal in context.evidence if signal.success
        }
        required_evidence = _required_evidence(intent)
        if not required_evidence.issubset(successful_evidence):
            return _deny(risk, "missing_evidence")

        if isinstance(intent, RestartInstanceIntent):
            if policy.get("stateless") is not True:
                return _deny(risk, "stateful_target")
        elif isinstance(intent, ScaleOutIntent):
            capacity_failure = _scale_capacity_failure(context, policy)
            if capacity_failure is not None:
                return _deny(risk, capacity_failure)
        elif isinstance(intent, RollbackRevisionIntent):
            revisions = policy.get("last_known_good_revisions")
            if not isinstance(revisions, list) or intent.revision not in revisions:
                return _deny(risk, "action_not_allowed")

        if context.mode is RemediationMode.DRY_RUN:
            return _allow(risk)
        if context.mode is RemediationMode.ASSISTED:
            return _approval(risk)
        if isinstance(intent, RollbackRevisionIntent):
            return _approval(risk)
        if (
            isinstance(intent, ScaleOutIntent)
            and context.live_replica_count is not None
            and context.live_replica_count < 4
        ):
            return _approval(risk)
        return _allow(risk)


def _risk_for(intent: ActionIntent) -> RiskLevel:
    if intent.action_type is ActionType.RESTART_INSTANCE:
        return RiskLevel.R1
    if intent.action_type is ActionType.SCALE_OUT:
        return RiskLevel.R2
    return RiskLevel.R3


def _required_evidence(intent: ActionIntent) -> set[str]:
    if isinstance(intent, RestartInstanceIntent):
        return {"alert", "metric"}
    if isinstance(intent, ScaleOutIntent):
        return {"metric"}
    return {"metric", "log"}


def _scale_capacity_failure(
    context: PolicyContext,
    policy: Mapping[str, Any],
) -> Literal["missing_target_state", "capacity_exceeded"] | None:
    current = context.live_replica_count
    maximum = policy.get("max_replicas")
    if (
        not isinstance(current, int)
        or isinstance(current, bool)
        or current < 1
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 1
    ):
        return "missing_target_state"
    if current + 1 > maximum:
        return "capacity_exceeded"
    return None


def _deny(risk: RiskLevel, reason_code: str) -> PolicyDecision:
    return PolicyDecision(
        verdict=PolicyVerdict.DENY,
        risk_level=risk,
        reason_code=reason_code,
    )


def _approval(risk: RiskLevel) -> PolicyDecision:
    return PolicyDecision(
        verdict=PolicyVerdict.REQUIRE_APPROVAL,
        risk_level=risk,
        reason_code="approval_required",
    )


def _allow(risk: RiskLevel) -> PolicyDecision:
    return PolicyDecision(
        verdict=PolicyVerdict.ALLOW,
        risk_level=risk,
        reason_code="policy_allowed",
    )
