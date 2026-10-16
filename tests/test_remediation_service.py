import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.harness.contracts import GatewayEvent, RemediationMode, RestartInstanceIntent
from app.repositories.remediation import InMemoryRemediationRepository
from app.services.remediation_service import (
    RemediationConflict,
    RemediationExpired,
    RemediationService,
)
from app.services.service_catalog import ServiceCatalog

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def build_catalog(tmp_path, *, enabled=True):
    catalog_path = tmp_path / "services.json"
    catalog_path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {
                        "remediation": {
                            "enabled": enabled,
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
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return ServiceCatalog(str(catalog_path))


def build_service(tmp_path, mode=RemediationMode.DRY_RUN, *, enabled=True):
    repository = InMemoryRemediationRepository()
    service = RemediationService(
        repository=repository,
        catalog=build_catalog(tmp_path, enabled=enabled),
        mode=mode,
        now_factory=lambda: NOW,
    )
    return service, repository


def restart_intent():
    return RestartInstanceIntent(
        action_type="restart_instance",
        service_name="orders",
        instance_id="orders-1",
        reason="The instance is unhealthy.",
    )


def second_restart_intent():
    return RestartInstanceIntent(
        action_type="restart_instance",
        service_name="orders",
        instance_id="orders-2",
        reason="A second instance is unhealthy.",
    )


def evidence(*, now=NOW, instance="orders-1"):
    collected = (now - timedelta(seconds=30)).isoformat()
    return [
        {"evidence_type": "alert", "source": "alertmanager", "success": True,
         "collected_at": collected, "summary": "Instance health alert is firing.", "payload": {"success": True, "raw": "A" * 10000,
         "alerts": [{"state": "firing", "labels": {"service": "orders", "instance": instance}}]}},
        {"evidence_type": "metric", "source": "prometheus", "success": True,
         "collected_at": collected, "summary": "Availability is below the guard.", "payload": {"success": True, "raw": "M" * 10000,
         "service_name": "orders", "instance_id": instance,
         "data_points": [{"timestamp": collected, "value": 0.5}]}},
    ]


@pytest.mark.asyncio
async def test_dry_run_proposal_is_enqueued_with_bounded_evidence_snapshot(tmp_path):
    service, repository = build_service(tmp_path)

    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    assert action["status"] == "proposed"
    assert len(repository.outbox_events) == 1
    assert len(action["evidence_snapshot"]) == 2
    for item in action["evidence_snapshot"]:
        assert set(item) == {
            "evidence_type",
            "source",
            "success",
            "collected_at",
            "sha256",
            "summary", "validation_reason", "validated_at", "valid_until",
            "target_service", "target_instance",
        }
        assert len(item["sha256"]) == 64
        assert len(item["summary"]) <= 256
        assert "payload" not in item
        assert "A" * 100 not in str(item)
        assert "M" * 100 not in str(item)


@pytest.mark.asyncio
async def test_assisted_proposal_waits_for_approval_without_outbox(tmp_path):
    service, repository = build_service(tmp_path, RemediationMode.ASSISTED)

    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    assert action["status"] == "awaiting_approval"
    assert action["approval_expires_at"] == NOW + timedelta(seconds=570)
    assert repository.outbox_events == []


@pytest.mark.asyncio
async def test_denied_proposal_is_persisted_but_never_enqueued(tmp_path):
    service, repository = build_service(tmp_path, enabled=False)

    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    assert action["status"] == "denied"
    assert action["policy_reason"] == "service_disabled"
    assert repository.outbox_events == []


@pytest.mark.asyncio
async def test_approval_is_atomic_and_enqueues_once(tmp_path):
    service, repository = build_service(tmp_path, RemediationMode.ASSISTED)
    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    approved = await service.decide(action["id"], "approve", "operator-1", NOW)

    assert approved["status"] == "approved"
    assert len(repository.outbox_events) == 1
    with pytest.raises(RemediationConflict):
        await service.decide(action["id"], "approve", "operator-1", NOW)
    assert len(repository.outbox_events) == 1


@pytest.mark.asyncio
async def test_concurrent_approve_reject_expire_has_one_outcome(tmp_path):
    service, repository = build_service(tmp_path, RemediationMode.ASSISTED)
    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    outcomes = await asyncio.gather(
        service.decide(action["id"], "approve", "operator-a", NOW),
        service.decide(action["id"], "reject", "operator-b", NOW),
        service.decide(
            action["id"],
            "approve",
            "operator-c",
            NOW + timedelta(minutes=16),
        ),
        return_exceptions=True,
    )

    successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    assert len(successes) == 1
    persisted = await repository.get(action["id"])
    assert persisted["status"] in {"approved", "rejected", "expired"}
    assert len(repository.outbox_events) <= 1


@pytest.mark.asyncio
async def test_expired_or_duplicate_decision_is_rejected(tmp_path):
    service, _repository = build_service(tmp_path, RemediationMode.ASSISTED)
    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    with pytest.raises(RemediationExpired):
        await service.decide(
            action["id"],
            "approve",
            "operator-1",
            NOW + timedelta(minutes=16),
        )
    with pytest.raises(RemediationExpired):
        await service.decide(action["id"], "reject", "operator-1", NOW)


@pytest.mark.asyncio
async def test_illegal_gateway_transition_is_rejected(tmp_path):
    service, _repository = build_service(tmp_path)
    action = await service.propose("diagnosis-1", restart_intent(), evidence())

    with pytest.raises(RemediationConflict):
        await service.apply_gateway_event(action["id"], GatewayEvent(status="succeeded"))


@pytest.mark.asyncio
async def test_proposal_checks_and_create_run_inside_repository_guard(tmp_path):
    class GuardedRepository(InMemoryRemediationRepository):
        def __init__(self):
            super().__init__()
            self.in_guard = False

        @asynccontextmanager
        async def proposal_guard(self):
            assert not self.in_guard
            self.in_guard = True
            try:
                yield
            finally:
                self.in_guard = False

        async def count_active(self, target_service=None):
            assert self.in_guard
            return await super().count_active(target_service)

        async def create(self, values, enqueue, **kwargs):
            assert self.in_guard
            return await super().create(values, enqueue, **kwargs)

    repository = GuardedRepository()
    service = RemediationService(
        repository=repository,
        catalog=build_catalog(tmp_path),
        now_factory=lambda: NOW,
    )

    action = await service.propose("diagnosis-guard", restart_intent(), evidence())

    assert action["status"] == "proposed"


@pytest.mark.asyncio
async def test_expired_approval_is_swept_before_capacity_check(tmp_path):
    service, repository = build_service(tmp_path, RemediationMode.ASSISTED)
    expired = await service.propose("diagnosis-old", restart_intent(), evidence())
    service._now = lambda: NOW + timedelta(minutes=16)

    current = await service.propose("diagnosis-new", second_restart_intent(), evidence(now=service._now(), instance="orders-2"))

    assert (await repository.get(expired["id"]))["status"] == "expired"
    assert current["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_rollback_failure_opens_circuit_and_blocks_new_proposals(tmp_path):
    service, repository = build_service(tmp_path)
    action = await service.propose("diagnosis-circuit", restart_intent(), evidence())
    await service.apply_gateway_event(action["id"], GatewayEvent(status="executing"))
    await service.apply_gateway_event(action["id"], GatewayEvent(status="verifying"))
    await service.apply_gateway_event(action["id"], GatewayEvent(status="rolling_back"))
    await service.apply_gateway_event(
        action["id"], GatewayEvent(status="failed", error="rollback failed")
    )
    service._now = lambda: NOW + timedelta(hours=1)

    blocked = await service.propose("diagnosis-after-circuit", second_restart_intent(), evidence(now=service._now(), instance="orders-2"))

    assert await repository.is_circuit_open("orders") is True
    assert blocked["status"] == "denied"
    assert blocked["policy_reason"] == "circuit_breaker_open"
