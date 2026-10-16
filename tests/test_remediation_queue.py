import json
from datetime import UTC, datetime, timedelta

import pytest

from app.config import config
from app.harness.contracts import RestartInstanceIntent
from app.harness.gateway import DryRunRemediationAdapter, RemediationGateway
from app.remediation_celery_app import celery_app
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


def action_values(action_id="action-1", **overrides):
    now = datetime.now(UTC)
    remediation_intent = intent()
    values = {
        "id": action_id,
        "diagnosis_id": "diagnosis-1",
        "idempotency_key": f"diagnosis-1:{action_id}",
        "action_type": "restart_instance",
        "target_service": "orders",
        "risk_level": "r1",
        "status": "proposed",
        "intent_payload": remediation_intent.model_dump(mode="json"),
        "evidence_snapshot": [
            {
                "evidence_type": kind, "source": "trusted-observer", "success": True,
                "validation_reason": "valid", "target_service": "orders",
                "target_instance": "orders-1", "collected_at": now.isoformat(),
                "validated_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=10)).isoformat(),
            }
            for kind in ("alert", "metric")
        ],
        "policy_decision": "allow",
        "policy_reason": "policy_allowed",
        "requested_by": "agent",
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return values


class CountingAdapter(DryRunRemediationAdapter):
    def __init__(self):
        self.prepare_calls = 0
        self.execute_calls = 0
        self.observe_calls = 0

    async def prepare(self, remediation_intent):
        self.prepare_calls += 1
        return await super().prepare(remediation_intent)

    async def execute(self, prepared):
        self.execute_calls += 1
        return await super().execute(prepared)

    async def observe(self, prepared):
        self.observe_calls += 1
        return await super().observe(prepared)


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

    first = await execute_remediation("action-1", "worker-1", service=service, gateway=gateway)
    duplicate = await execute_remediation("action-1", "worker-2", service=service, gateway=gateway)

    assert first == "succeeded"
    assert duplicate == "ignored"
    assert adapter.execute_calls == 1


@pytest.mark.asyncio
async def test_worker_applies_each_gateway_event_in_order(tmp_path):
    repository, service, _adapter, gateway = build_worker_dependencies(tmp_path)
    await repository.create(action_values(), enqueue=False)
    observed = []
    original_apply = service.apply_gateway_event

    async def recording_apply(action_id, event, **kwargs):
        observed.append(event.status.value)
        return await original_apply(action_id, event, **kwargs)

    service.apply_gateway_event = recording_apply

    result = await execute_remediation("action-1", "worker-1", service=service, gateway=gateway)

    assert result == "succeeded"
    assert observed == ["executing", "verifying", "succeeded"]


@pytest.mark.asyncio
async def test_worker_reconciles_stale_prepared_action_without_second_execute(
    tmp_path, monkeypatch
):
    repository, service, adapter, gateway = build_worker_dependencies(tmp_path)
    now = datetime(2026, 10, 13, 12, 10, tzinfo=UTC)
    prepared = await adapter.prepare(intent())
    await repository.create(
        action_values(
            status="executing",
            prepared_payload=prepared.model_dump(mode="json"),
            updated_at=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        ),
        enqueue=False,
    )
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: now)
    monkeypatch.setattr(config, "remediation_worker_lease_seconds", 300)

    result = await execute_remediation(
        "action-1", "recovery-worker", service=service, gateway=gateway
    )

    assert result == "succeeded"
    assert adapter.execute_calls == 0
    assert adapter.observe_calls == 1


@pytest.mark.asyncio
async def test_off_mode_denies_queued_action_before_adapter_execution(tmp_path, monkeypatch):
    repository, service, adapter, gateway = build_worker_dependencies(tmp_path)
    await repository.create(action_values(), enqueue=False)
    monkeypatch.setattr(config, "remediation_mode", "off")

    result = await execute_remediation("action-1", "worker-1", service=service, gateway=gateway)

    assert result == "disabled"
    assert adapter.execute_calls == 0
    assert (await repository.get("action-1"))["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "approved"])
async def test_worker_never_prepares_or_executes_legacy_unvalidated_action(tmp_path, status):
    repository, service, adapter, gateway = build_worker_dependencies(tmp_path)
    await repository.create(action_values(status=status, evidence_snapshot=[]), enqueue=True)

    result = await execute_remediation("action-1", "worker", service=service, gateway=gateway)

    assert result == "ignored"
    assert (await repository.get("action-1"))["status"] == "denied"
    assert await repository.count_active() == 0
    assert adapter.prepare_calls == adapter.execute_calls == adapter.observe_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "approved"])
@pytest.mark.parametrize("invalidity", ["expired", "wrong_service", "wrong_instance"])
async def test_worker_rejects_frozen_evidence_before_any_adapter_call(tmp_path, monkeypatch, status, invalidity):
    repository, service, adapter, gateway = build_worker_dependencies(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr("app.repositories.remediation.utc_now", lambda: now)
    values = action_values(status=status)
    for item in values["evidence_snapshot"]:
        if invalidity == "expired":
            item["valid_until"] = now.isoformat()
        elif invalidity == "wrong_service":
            item["target_service"] = "payments"
        else:
            item["target_instance"] = "orders-2"
    await repository.create(values, enqueue=True)

    result = await execute_remediation("action-1", "worker", service=service, gateway=gateway)

    assert result == "ignored"
    action = await repository.get("action-1")
    assert action["status"] == "denied"
    assert action["error"].startswith("frozen_evidence_invalid_or_expired")
    assert await repository.count_active() == 0
    assert adapter.prepare_calls == adapter.execute_calls == adapter.observe_calls == 0


def test_remediation_worker_has_minimal_environment_and_runtime_controls():
    compose = json.loads(
        __import__("subprocess").check_output(  # nosec B404,B603 - test-only compose parser
            ["docker", "compose", "-f", "vector-database.yml", "config", "--format", "json"],
            text=True,
        )
    )
    environment = compose["services"]["remediation-worker"]["environment"]

    assert environment["REMEDIATION_MODE"] == "dry_run"
    assert environment["REMEDIATION_ADAPTER"] == "dry_run"
    assert "REMEDIATION_WORKER_LEASE_SECONDS" in environment
    assert "DASHSCOPE_API_KEY" not in environment
    assert "TENCENTCLOUD_SECRET_ID" not in environment
    assert "TENCENTCLOUD_SECRET_KEY" not in environment
