"""Closed, bounded contracts for remediation proposals and execution events."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

ServiceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
InstanceId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
Revision = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
ReasonCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,127}$"),
]
OpaqueToken = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class RemediationMode(StrEnum):
    OFF = "off"
    DRY_RUN = "dry_run"
    ASSISTED = "assisted"
    GUARDED_AUTO = "guarded_auto"


class ActionType(StrEnum):
    RESTART_INSTANCE = "restart_instance"
    SCALE_OUT = "scale_out"
    ROLLBACK_REVISION = "rollback_revision"


class RiskLevel(StrEnum):
    R1 = "r1"
    R2 = "r2"
    R3 = "r3"
    R4 = "r4"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    RECONCILING = "reconciling"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DENIED = "denied"


class PolicyVerdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RestartInstanceIntent(StrictContract):
    action_type: Literal[ActionType.RESTART_INSTANCE]
    service_name: ServiceName
    instance_id: InstanceId
    reason: Reason


class ScaleOutIntent(StrictContract):
    action_type: Literal[ActionType.SCALE_OUT]
    service_name: ServiceName
    replicas_delta: Literal[1] = 1
    reason: Reason


class RollbackRevisionIntent(StrictContract):
    action_type: Literal[ActionType.ROLLBACK_REVISION]
    service_name: ServiceName
    revision: Revision
    reason: Reason


ActionIntent = Annotated[
    RestartInstanceIntent | ScaleOutIntent | RollbackRevisionIntent,
    Field(discriminator="action_type"),
]
_ACTION_INTENT_ADAPTER = TypeAdapter(ActionIntent)


def parse_action_intent(payload: dict[str, Any]) -> ActionIntent:
    """Validate an untrusted model proposal against the closed intent union."""
    return _ACTION_INTENT_ADAPTER.validate_python(payload)


class PolicyDecision(StrictContract):
    verdict: PolicyVerdict
    risk_level: RiskLevel
    reason_code: ReasonCode


class PreparedAction(StrictContract):
    intent: ActionIntent
    pre_change_snapshot: dict[str, Any]
    verification_spec: dict[str, Any]
    rollback_token: OpaqueToken


class ExecutionResult(StrictContract):
    success: bool
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(StrictContract):
    success: bool
    observation: dict[str, Any] = Field(default_factory=dict)
    reason: Reason | None = None


class GatewayEvent(StrictContract):
    status: ActionStatus
    prepared_action: PreparedAction | None = None
    result: ExecutionResult | None = None
    verification: VerificationResult | None = None
    error: Reason | None = None
