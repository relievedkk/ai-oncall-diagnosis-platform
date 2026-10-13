"""Bounded public models for remediation review and decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.harness.contracts import (
    ActionIntent,
    ActionStatus,
    ActionType,
    PolicyVerdict,
    RiskLevel,
)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class EvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_type: str = Field(max_length=64)
    source: str = Field(max_length=128)
    success: bool
    collected_at: str = Field(max_length=64)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(max_length=256)


class RemediationActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    diagnosis_id: str
    action_type: ActionType
    target_service: str
    risk_level: RiskLevel
    status: ActionStatus
    intent: ActionIntent
    evidence: list[EvidenceSummary] = Field(max_length=20)
    policy_decision: PolicyVerdict
    policy_reason: str
    requested_by: str
    approved_by: str | None = None
    approval_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


class RemediationListResponse(BaseModel):
    items: list[RemediationActionResponse]
