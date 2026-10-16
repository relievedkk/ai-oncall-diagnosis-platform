"""A durable proposal loses authority when its supporting evidence expires."""

from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)
INTENT = {"action_type": "restart_instance", "service_name": "orders", "instance_id": "orders-1"}


def snapshot(kind, **overrides):
    return {
        "evidence_type": kind, "source": "trusted-monitor", "success": True,
        "validation_reason": "valid", "target_service": "orders", "target_instance": "orders-1",
        "collected_at": (NOW - timedelta(seconds=20)).isoformat(),
        "validated_at": NOW.isoformat(), "valid_until": (NOW + timedelta(seconds=580)).isoformat(),
        **overrides,
    }


@pytest.mark.parametrize("overrides", [
    {"success": False}, {"validation_reason": "target_mismatch"}, {"target_service": "payments"},
    {"target_instance": "orders-2"}, {"valid_until": NOW.isoformat()},
    {"collected_at": "2000-01-01T00:00:00Z"}, {"validated_at": "2030-01-01T00:00:00Z"},
    {"valid_until": "2030-01-01T00:00:00Z"}, {"valid_until": "invalid"}, {"source": ""},
])
def test_invalid_frozen_support_cannot_authorize(overrides):
    from app.core.remediation_evidence import frozen_evidence_authorizes

    assert not frozen_evidence_authorizes(INTENT, [snapshot("alert"), snapshot("metric", **overrides)], NOW)


def test_authority_deadline_needs_one_current_signal_of_each_required_type():
    from app.core.remediation_evidence import evidence_authority_deadline

    evidence = [snapshot("alert", valid_until=(NOW + timedelta(seconds=30)).isoformat()), snapshot("metric")]
    assert evidence_authority_deadline(INTENT, evidence, NOW) == NOW + timedelta(seconds=30)
    evidence.append(snapshot("alert", valid_until=(NOW + timedelta(seconds=90)).isoformat()))
    assert evidence_authority_deadline(INTENT, evidence, NOW) == NOW + timedelta(seconds=90)
    assert evidence_authority_deadline(INTENT, [snapshot("metric")], NOW) is None


def test_legacy_snapshot_without_validation_metadata_has_no_execution_authority():
    from app.core.remediation_evidence import frozen_evidence_authorizes

    assert not frozen_evidence_authorizes(INTENT, [{"evidence_type": "metric", "success": True}], NOW)
