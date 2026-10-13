"""Authenticated remediation history and human decision endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.harness.contracts import ActionStatus
from app.models.remediation import (
    DecisionRequest,
    RemediationActionResponse,
    RemediationListResponse,
)
from app.services.remediation_service import (
    RemediationConflict,
    RemediationExpired,
    RemediationNotFound,
    remediation_service,
)

router = APIRouter()

_SENSITIVE_KEY_PARTS = {
    "authorization",
    "command",
    "credential",
    "environment",
    "header",
    "password",
    "secret",
    "token",
    "url",
}


def _safe_payload(value: Any, depth: int = 0) -> Any:
    """Return a small JSON-compatible diagnostic payload without credential-shaped keys."""
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:32]:
            key = str(raw_key)[:64]
            normalized = key.casefold()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                continue
            result[key] = _safe_payload(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_payload(item, depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:512]


def _response(action: dict[str, Any]) -> RemediationActionResponse:
    return RemediationActionResponse(
        id=str(action["id"]),
        diagnosis_id=str(action["diagnosis_id"]),
        action_type=action["action_type"],
        target_service=str(action["target_service"]),
        risk_level=action["risk_level"],
        status=action["status"],
        intent=action["intent_payload"],
        evidence=action.get("evidence_snapshot") or [],
        policy_decision=action["policy_decision"],
        policy_reason=str(action["policy_reason"]),
        requested_by=str(action["requested_by"]),
        approved_by=action.get("approved_by"),
        approval_expires_at=action.get("approval_expires_at"),
        created_at=action["created_at"],
        updated_at=action["updated_at"],
        approved_at=action.get("approved_at"),
        started_at=action.get("started_at"),
        completed_at=action.get("completed_at"),
        result=_safe_payload(action.get("result_payload")),
        verification=_safe_payload(action.get("verification_payload")),
    )


@router.get("/remediations", response_model=RemediationListResponse)
async def list_remediations(
    status_filter: ActionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=100),
) -> RemediationListResponse:
    actions = await remediation_service.list_actions(
        status=status_filter.value if status_filter is not None else None,
        limit=limit,
    )
    return RemediationListResponse(items=[_response(action) for action in actions])


@router.get("/remediations/{action_id}", response_model=RemediationActionResponse)
async def get_remediation(action_id: str) -> RemediationActionResponse:
    try:
        action = await remediation_service.get_action(action_id)
    except RemediationNotFound as exc:
        raise HTTPException(status_code=404, detail="Remediation action not found") from exc
    return _response(action)


@router.post(
    "/remediations/{action_id}/decision",
    response_model=RemediationActionResponse,
)
async def decide_remediation(
    action_id: str,
    payload: DecisionRequest,
    request: Request,
) -> RemediationActionResponse:
    principal = getattr(request.state, "principal", None)
    if getattr(request.state, "auth_kind", None) != "session" or principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A browser operator session is required",
        )
    try:
        action = await remediation_service.decide(
            action_id,
            payload.decision,
            str(principal.user_id),
        )
    except RemediationNotFound as exc:
        raise HTTPException(status_code=404, detail="Remediation action not found") from exc
    except RemediationExpired as exc:
        raise HTTPException(status_code=410, detail="Approval window expired") from exc
    except RemediationConflict as exc:
        raise HTTPException(status_code=409, detail="Remediation state conflict") from exc
    return _response(action)
