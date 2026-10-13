from datetime import UTC, datetime, timedelta

import pytest

from app.harness.contracts import (
    PolicyVerdict,
    RemediationMode,
    RestartInstanceIntent,
    RiskLevel,
    RollbackRevisionIntent,
    ScaleOutIntent,
)
from app.harness.policy import EvidenceSignal, PolicyContext, PolicyEngine

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def service_policy(**overrides):
    policy = {
        "enabled": True,
        "stateless": True,
        "allowed_actions": [
            "restart_instance",
            "scale_out",
            "rollback_revision",
        ],
        "max_replicas": 10,
        "cooldown_seconds": 900,
        "last_known_good_revisions": ["stable-42"],
    }
    policy.update(overrides)
    return policy


def context(mode=RemediationMode.DRY_RUN, **overrides):
    values = {
        "mode": mode,
        "service_policy": service_policy(),
        "evidence": (
            EvidenceSignal(evidence_type="alert", success=True),
            EvidenceSignal(evidence_type="metric", success=True),
            EvidenceSignal(evidence_type="log", success=True),
        ),
        "live_replica_count": 4,
        "now": NOW,
        "last_completed_action_at": None,
        "service_action_active": False,
        "global_active_count": 0,
        "diagnosis_action_count": 0,
    }
    values.update(overrides)
    return PolicyContext(**values)


def intent(action_type):
    if action_type == "restart_instance":
        return RestartInstanceIntent(
            action_type=action_type,
            service_name="orders",
            instance_id="orders-1",
            reason="The instance is unhealthy.",
        )
    if action_type == "scale_out":
        return ScaleOutIntent(
            action_type=action_type,
            service_name="orders",
            replicas_delta=1,
            reason="Sustained saturation is confirmed.",
        )
    return RollbackRevisionIntent(
        action_type=action_type,
        service_name="orders",
        revision="stable-42",
        reason="The current revision is regressing.",
    )


@pytest.mark.parametrize(
    ("mode", "action_type", "expected"),
    [
        *[
            (RemediationMode.OFF, action, PolicyVerdict.DENY)
            for action in ("restart_instance", "scale_out", "rollback_revision")
        ],
        *[
            (RemediationMode.DRY_RUN, action, PolicyVerdict.ALLOW)
            for action in ("restart_instance", "scale_out", "rollback_revision")
        ],
        *[
            (RemediationMode.ASSISTED, action, PolicyVerdict.REQUIRE_APPROVAL)
            for action in ("restart_instance", "scale_out", "rollback_revision")
        ],
        (
            RemediationMode.GUARDED_AUTO,
            "restart_instance",
            PolicyVerdict.ALLOW,
        ),
        (RemediationMode.GUARDED_AUTO, "scale_out", PolicyVerdict.ALLOW),
        (
            RemediationMode.GUARDED_AUTO,
            "rollback_revision",
            PolicyVerdict.REQUIRE_APPROVAL,
        ),
    ],
)
def test_policy_matrix_is_deterministic(mode, action_type, expected):
    decision = PolicyEngine().evaluate(intent(action_type), context(mode))

    assert decision.verdict is expected


def test_unknown_service_policy_is_denied():
    decision = PolicyEngine().evaluate(
        intent("restart_instance"),
        context(service_policy={"enabled": False, "allowed_actions": []}),
    )

    assert decision.verdict is PolicyVerdict.DENY
    assert decision.reason_code == "service_disabled"


def test_restart_requires_stateless_service_and_alert_metric_evidence():
    stateful = PolicyEngine().evaluate(
        intent("restart_instance"),
        context(service_policy=service_policy(stateless=False)),
    )
    missing_alert = PolicyEngine().evaluate(
        intent("restart_instance"),
        context(evidence=(EvidenceSignal(evidence_type="metric", success=True),)),
    )

    assert stateful.reason_code == "stateful_target"
    assert missing_alert.reason_code == "missing_evidence"


def test_scale_out_requires_metric_evidence_and_catalog_capacity():
    missing_metric = PolicyEngine().evaluate(
        intent("scale_out"),
        context(evidence=(EvidenceSignal(evidence_type="alert", success=True),)),
    )
    unknown_count = PolicyEngine().evaluate(
        intent("scale_out"),
        context(live_replica_count=None),
    )
    at_capacity = PolicyEngine().evaluate(
        intent("scale_out"),
        context(live_replica_count=10),
    )

    assert missing_metric.reason_code == "missing_evidence"
    assert unknown_count.reason_code == "missing_target_state"
    assert at_capacity.reason_code == "capacity_exceeded"


def test_guarded_auto_scale_out_requires_live_replica_count_of_at_least_four():
    decision = PolicyEngine().evaluate(
        intent("scale_out"),
        context(RemediationMode.GUARDED_AUTO, live_replica_count=3),
    )

    assert decision.verdict is PolicyVerdict.REQUIRE_APPROVAL
    assert decision.reason_code == "approval_required"


def test_new_rollback_always_requires_approval():
    decision = PolicyEngine().evaluate(
        intent("rollback_revision"),
        context(RemediationMode.GUARDED_AUTO),
    )

    assert decision.verdict is PolicyVerdict.REQUIRE_APPROVAL
    assert decision.risk_level is RiskLevel.R3


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"service_action_active": True}, "service_busy"),
        ({"global_active_count": 5}, "budget_exhausted"),
        ({"diagnosis_action_count": 2}, "budget_exhausted"),
        (
            {"last_completed_action_at": NOW - timedelta(seconds=899)},
            "cooldown_active",
        ),
    ],
)
def test_active_action_cooldown_and_diagnosis_budget_fail_closed(overrides, reason_code):
    decision = PolicyEngine().evaluate(
        intent("restart_instance"),
        context(**overrides),
    )

    assert decision.verdict is PolicyVerdict.DENY
    assert decision.reason_code == reason_code


def test_model_confidence_cannot_lower_risk():
    decision = PolicyEngine().evaluate(
        intent("rollback_revision"),
        context(service_policy=service_policy(model_confidence=1.0)),
    )

    assert decision.risk_level is RiskLevel.R3


def test_open_service_circuit_fails_closed():
    decision = PolicyEngine().evaluate(
        intent("restart_instance"), context(service_circuit_open=True)
    )

    assert decision.verdict is PolicyVerdict.DENY
    assert decision.reason_code == "circuit_breaker_open"
