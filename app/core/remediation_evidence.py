"""Recheck frozen evidence authority when approving or claiming an action."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import config

_REQUIRED = {
    "restart_instance": {"alert", "metric"},
    "scale_out": {"metric"},
    "rollback_revision": {"metric", "log"},
}


def _time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return None


def evidence_authority_deadline(
    intent_payload: dict[str, Any], snapshot: Any, now: datetime,
) -> datetime | None:
    """Each required type needs a still-valid, attributable target observation.

    Older actions lacking validation metadata fail closed. This verifies the
    immutable proposal's grant; it does not fetch new infrastructure state.
    """
    if not isinstance(intent_payload, dict) or not isinstance(snapshot, list) or now.tzinfo is None:
        return None
    action = intent_payload.get("action_type")
    if not isinstance(action, str) or action not in _REQUIRED:
        return None
    service = intent_payload.get("service_name")
    instance = intent_payload.get("instance_id")
    if not isinstance(service, str) or not service.strip():
        return None
    if action == "restart_instance" and (not isinstance(instance, str) or not instance):
        return None
    max_age = timedelta(seconds=config.remediation_evidence_max_age_seconds)
    skew = timedelta(seconds=config.remediation_evidence_clock_skew_seconds)
    deadlines: dict[str, datetime] = {}
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        kind = item.get("evidence_type")
        if not isinstance(kind, str) or kind not in _REQUIRED[action]:
            continue
        if (
            item.get("success") is not True or item.get("validation_reason") != "valid"
            or item.get("target_service") != service
            or (action == "restart_instance" and item.get("target_instance") != instance)
            or not isinstance(item.get("source"), str) or not item["source"].strip()
        ):
            continue
        collected, validated, until = (
            _time(item.get(key)) for key in ("collected_at", "validated_at", "valid_until")
        )
        if (
            collected is None or validated is None or until is None
            or collected > now + skew or validated > now + skew
            or now - collected > max_age or now - validated > max_age
            or until <= now or until > min(collected, validated) + max_age + skew
        ):
            continue
        deadlines[kind] = max(deadlines.get(kind, until), until)
    if not _REQUIRED[action].issubset(deadlines):
        return None
    return min(deadlines[kind] for kind in _REQUIRED[action])


def frozen_evidence_authorizes(intent_payload: dict[str, Any], snapshot: Any, now: datetime) -> bool:
    return evidence_authority_deadline(intent_payload, snapshot, now) is not None
