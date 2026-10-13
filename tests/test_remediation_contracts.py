import pytest
from pydantic import ValidationError

from app.harness.contracts import (
    RestartInstanceIntent,
    RollbackRevisionIntent,
    ScaleOutIntent,
    parse_action_intent,
)


def test_parse_restart_intent_rejects_extra_command_field():
    with pytest.raises(ValidationError):
        parse_action_intent(
            {
                "action_type": "restart_instance",
                "service_name": "orders",
                "instance_id": "orders-7f9d",
                "reason": "The instance is unhealthy.",
                "command": "kubectl delete pod orders-7f9d",
            }
        )

    intent = parse_action_intent(
        {
            "action_type": "restart_instance",
            "service_name": "orders",
            "instance_id": "orders-7f9d",
            "reason": "The instance is unhealthy.",
        }
    )
    assert isinstance(intent, RestartInstanceIntent)


def test_scale_out_requires_positive_delta_and_caps_input_at_one():
    valid = parse_action_intent(
        {
            "action_type": "scale_out",
            "service_name": "orders",
            "replicas_delta": 1,
            "reason": "Sustained saturation is confirmed by metrics.",
        }
    )
    assert isinstance(valid, ScaleOutIntent)
    assert valid.replicas_delta == 1

    for invalid_delta in (0, -1, 2):
        with pytest.raises(ValidationError):
            parse_action_intent(
                {
                    "action_type": "scale_out",
                    "service_name": "orders",
                    "replicas_delta": invalid_delta,
                    "reason": "Sustained saturation is confirmed by metrics.",
                }
            )


def test_rollback_requires_nonempty_revision():
    with pytest.raises(ValidationError):
        RollbackRevisionIntent(
            action_type="rollback_revision",
            service_name="orders",
            revision="",
            reason="The current revision is regressing.",
        )


def test_action_payload_rejects_oversized_target_and_reason():
    invalid_payloads = (
        {
            "action_type": "restart_instance",
            "service_name": "s" * 129,
            "instance_id": "instance-1",
            "reason": "unhealthy",
        },
        {
            "action_type": "restart_instance",
            "service_name": "orders",
            "instance_id": "i" * 257,
            "reason": "unhealthy",
        },
        {
            "action_type": "rollback_revision",
            "service_name": "orders",
            "revision": "r" * 129,
            "reason": "regression",
        },
        {
            "action_type": "scale_out",
            "service_name": "orders",
            "replicas_delta": 1,
            "reason": "r" * 513,
        },
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            parse_action_intent(payload)

