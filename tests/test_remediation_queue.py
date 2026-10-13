import json
from datetime import UTC, datetime

import pytest

from app.celery_app import celery_app
from app.config import config
from app.harness.contracts import RestartInstanceIntent
from app.harness.gateway import DryRunRemediationAdapter, RemediationGateway
from app.repositories.remediation import InMemoryRemediationRepository
from app.services.remediation_service import RemediationService
from app.services.service_catalog import ServiceCatalog
from app.tasks.remediation import execute_remediation
from app.workers import outbox_publisher


class FakeOutboxRepository:
    def __init__(self, events):
        self.events = events
        self.published = []
        self.failed = []

    async def claim_outbox(self, publisher_id):
        return list(self.events)

    async def mark_outbox_published(self, event_id):
        self.published.append(event_id)

    async def mark_outbox_failed(self, event_id, error):
        self.failed.append((event_id, error))


@pytest.mark.asyncio
async def test_outbox_publisher_sends_generic_args_and_task_id(monkeypatch):
    repository = FakeOutboxRepository(
        [
            {
                "id": 7,
                "task_name": "app.tasks.remediation.run_remediation",
                "queue_name": config.celery_remediation_queue,
                "payload": {"args": ["action-7"], "task_id": "action-7"},
            }
        ]
    )
    calls = []

    def fake_send_task(name, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(outbox_publisher, "diagnosis_repository", repository)
    monkeypatch.setattr(outbox_publisher.celery_app, "send_task", fake_send_task)

    published = await outbox_publisher.publish_once("publisher-1")

    assert published == 1
    assert calls == [
        (
            "app.tasks.remediation.run_remediation",
            {
                "args": ["action-7"],
                "task_id": "action-7",
                "queue": config.celery_remediation_queue,
                "routing_key": "remediation",
            },
        )
    ]
    assert repository.published == [7]
    assert repository.failed == []


def test_remediation_queue_is_durable_quorum_queue():
    queue = next(
        queue
        for queue in celery_app.conf.task_queues
        if queue.name == config.celery_remediation_queue
    )

    assert queue.durable is True
    assert queue.routing_key == "remediation"
    assert queue.queue_arguments == {"x-queue-type": "quorum"}


def build_catalog(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {
                        "remediation": {
                            "enabled": True,
                            "stateless": True,
                            "allowed_actions": ["restart_instance"],
                            "cooldown_seconds": 0,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return ServiceCatalog(str(path))


def intent():
    return RestartInstanceIntent(
        action_type="restart_instance",
        service_name="orders",
        instance_id="orders-1",
        reason="The instance is unhealthy.",
    )


def action_values(action_id="action-1"):
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    remediation_intent = intent()
    return {
        "id": action_id,
        "diagnosis_id": "diagnosis-1",
        "idempotency_key": f"diagnosis-1:{action_id}",
        "action_type": "restart_instance",
        "target_service": "orders",
        "risk_level": "r1",
        "status": "proposed",
        "intent_payload": remediation_intent.model_dump(mode="json"),
        "evidence_snapshot": [],
        "policy_decision": "allow",
        "policy_reason": "policy_allowed",
        "requested_by": "agent",
        "created_at": now,
        "updated_at": now,
    }


class CountingAdapter(DryRunRemediationAdapter):
    def __init__(self):
        self.execute_calls = 0

    async def execute(self, prepared):
        self.execute_calls += 1
        return await super().execute(prepared)


def build_worker_dependencies(tmp_path):
    repository = InMemoryRemediationRepository()
    service = RemediationService(repository=repository, catalog=build_catalog(tmp_path))
    adapter = CountingAdapter()
    gateway = RemediationGateway(adapter)
    return repository, service, adapter, gateway


@pytest.mark.asyncio
async def test_duplicate_task_delivery_claims_and_runs_adapter_once(tmp_path):
    repository, service, adapter, gateway = build_worker_dependencies(tmp_path)
    await repository.create(action_values(), enqueue=False)

    first = await execute_remediation(
        "action-1", "worker-1", service=service, gateway=gateway
    )
    duplicate = await execute_remediation(
        "action-1", "worker-2", service=service, gateway=gateway
    )

    assert first == "succeeded"
    assert duplicate == "ignored"
    assert adapter.execute_calls == 1


@pytest.mark.asyncio
async def test_worker_applies_each_gateway_event_in_order(tmp_path):
    repository, service, _adapter, gateway = build_worker_dependencies(tmp_path)
    await repository.create(action_values(), enqueue=False)
    observed = []
    original_apply = service.apply_gateway_event

    async def recording_apply(action_id, event):
        observed.append(event.status.value)
        return await original_apply(action_id, event)

    service.apply_gateway_event = recording_apply

    result = await execute_remediation(
        "action-1", "worker-1", service=service, gateway=gateway
    )

    assert result == "succeeded"
    assert observed == ["executing", "verifying", "succeeded"]

