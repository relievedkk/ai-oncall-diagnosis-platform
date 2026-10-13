from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import remediation as remediation_api
from app.config import config
from app.core.auth import ApiKeyMiddleware
from app.services.auth_service import AuthService, SessionPrincipal, auth_service
from app.services.remediation_service import (
    RemediationConflict,
    RemediationExpired,
    RemediationNotFound,
)


def _action(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    action: dict[str, object] = {
        "id": "action-1",
        "diagnosis_id": "diagnosis-1",
        "idempotency_key": "must-not-leak",
        "action_type": "restart_instance",
        "target_service": "payments",
        "risk_level": "r2",
        "status": "awaiting_approval",
        "intent_payload": {
            "action_type": "restart_instance",
            "service_name": "payments",
            "instance_id": "payments-1",
            "reason": "high error rate",
        },
        "evidence_snapshot": [
            {
                "evidence_type": "metric",
                "source": "prometheus",
                "success": True,
                "collected_at": now.isoformat(),
                "sha256": "a" * 64,
                "summary": "5xx rate is elevated",
                "raw_payload": {"password": "raw-secret"},
            }
        ],
        "policy_decision": "require_approval",
        "policy_reason": "restart_requires_approval",
        "prepared_payload": {"rollback_token": "adapter-secret"},
        "result_payload": {
            "success": True,
            "details": {"message": "dry run", "api_token": "adapter-secret"},
        },
        "verification_payload": {
            "success": True,
            "observation": {"healthy": True, "password": "adapter-secret"},
        },
        "requested_by": "agent",
        "approved_by": None,
        "worker_name": "worker-secret",
        "approval_expires_at": now + timedelta(minutes=10),
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "started_at": None,
        "completed_at": None,
        "error": None,
    }
    action.update(overrides)
    return action


def _principal(csrf_token: str = "csrf-value") -> SessionPrincipal:
    return SessionPrincipal(
        user_id=7,
        username="operator",
        display_name="Operator",
        role="operator",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        csrf_hash=AuthService.hash_secret(csrf_token),
    )


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "web_login_enabled", True)
    monkeypatch.setattr(config, "api_key", "a" * 64)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)
    app.include_router(remediation_api.router, prefix="/api")
    return TestClient(app)


def test_list_and_detail_require_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_actions(status: str | None = None, limit: int = 100):
        return [_action()]

    async def get_action(_action_id: str):
        return _action()

    monkeypatch.setattr(remediation_api.remediation_service, "list_actions", list_actions)
    monkeypatch.setattr(remediation_api.remediation_service, "get_action", get_action)
    client = _client(monkeypatch)

    assert client.get("/api/remediations").status_code == 401
    assert client.get("/api/remediations/action-1").status_code == 401
    headers = {"X-API-Key": "a" * 64}
    assert client.get("/api/remediations", headers=headers).status_code == 200
    assert client.get("/api/remediations/action-1", headers=headers).status_code == 200


def test_machine_api_key_cannot_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    response = client.post(
        "/api/remediations/action-1/decision",
        headers={"X-API-Key": "a" * 64},
        json={"decision": "approve"},
    )

    assert response.status_code == 403


def test_session_decision_requires_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_principal(_token: str):
        return _principal()

    async def decide(*_args, **_kwargs):
        return _action(status="approved", approved_by="7")

    monkeypatch.setattr(auth_service, "get_principal", get_principal)
    monkeypatch.setattr(remediation_api.remediation_service, "decide", decide)
    client = _client(monkeypatch)
    client.cookies.set(config.session_cookie_name, "session-value")
    client.cookies.set(config.csrf_cookie_name, "csrf-value")

    assert client.post(
        "/api/remediations/action-1/decision", json={"decision": "approve"}
    ).status_code == 403
    assert client.post(
        "/api/remediations/action-1/decision",
        headers={"X-CSRF-Token": "csrf-value"},
        json={"decision": "approve"},
    ).status_code == 200


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (RemediationNotFound("missing"), 404),
        (RemediationExpired("expired"), 410),
        (RemediationConflict("conflict"), 409),
    ],
)
def test_decision_error_mappings(
    monkeypatch: pytest.MonkeyPatch,
    exception: Exception,
    expected_status: int,
) -> None:
    async def get_principal(_token: str):
        return _principal()

    async def decide(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(auth_service, "get_principal", get_principal)
    monkeypatch.setattr(remediation_api.remediation_service, "decide", decide)
    client = _client(monkeypatch)
    client.cookies.set(config.session_cookie_name, "session-value")
    client.cookies.set(config.csrf_cookie_name, "csrf-value")

    response = client.post(
        "/api/remediations/action-1/decision",
        headers={"X-CSRF-Token": "csrf-value"},
        json={"decision": "reject"},
    )

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("decision", "status"),
    [("approve", "approved"), ("reject", "rejected")],
)
def test_approve_and_reject(
    monkeypatch: pytest.MonkeyPatch, decision: str, status: str
) -> None:
    async def get_principal(_token: str):
        return _principal()

    async def decide(_action_id: str, received: str, principal_id: str):
        assert received == decision
        assert principal_id == "7"
        return _action(status=status, approved_by="7")

    monkeypatch.setattr(auth_service, "get_principal", get_principal)
    monkeypatch.setattr(remediation_api.remediation_service, "decide", decide)
    client = _client(monkeypatch)
    client.cookies.set(config.session_cookie_name, "session-value")
    client.cookies.set(config.csrf_cookie_name, "csrf-value")

    response = client.post(
        "/api/remediations/action-1/decision",
        headers={"X-CSRF-Token": "csrf-value"},
        json={"decision": decision},
    )

    assert response.status_code == 200
    assert response.json()["status"] == status


def test_api_response_omits_raw_evidence_and_adapter_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_action(_action_id: str):
        return _action()

    monkeypatch.setattr(remediation_api.remediation_service, "get_action", get_action)
    response = _client(monkeypatch).get(
        "/api/remediations/action-1", headers={"X-API-Key": "a" * 64}
    )

    assert response.status_code == 200
    body = response.json()
    serialized = response.text
    assert body["evidence"][0]["summary"] == "5xx rate is elevated"
    assert "raw_payload" not in serialized
    assert "adapter-secret" not in serialized
    assert "rollback_token" not in serialized
    assert "worker-secret" not in serialized
    assert "must-not-leak" not in serialized


def test_list_limit_is_bounded_and_status_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_actions(status: str | None = None, limit: int = 100):
        return []

    monkeypatch.setattr(remediation_api.remediation_service, "list_actions", list_actions)
    client = _client(monkeypatch)
    headers = {"X-API-Key": "a" * 64}

    assert client.get("/api/remediations?limit=0", headers=headers).status_code == 422
    assert client.get("/api/remediations?limit=101", headers=headers).status_code == 422
    assert client.get("/api/remediations?status=bogus", headers=headers).status_code == 422
    assert client.get(
        "/api/remediations?status=awaiting_approval&limit=25", headers=headers
    ).status_code == 200
