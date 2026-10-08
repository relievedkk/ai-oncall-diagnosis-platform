from app.config import config
from app.models.alerts import AlertmanagerWebhook
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.alert_diagnosis_service import AlertDiagnosisService


def _payload() -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(
        {
            "receiver": "oncall-agent",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "HighHTTPErrorRate",
                        "severity": "critical",
                        "service": "ai-oncall-agent",
                        "job": "ai-oncall-agent",
                    },
                    "annotations": {"summary": "error rate high"},
                    "startsAt": "2026-10-08T10:00:00+08:00",
                    "fingerprint": "queue-test",
                }
            ],
        }
    )


async def test_alert_is_atomically_queued_without_in_process_execution():
    repository = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repository=repository, inline_worker=False)

    diagnosis_id = (await service.handle_webhook(_payload()))[0]
    job = await service.get_job(diagnosis_id)
    outbox = await repository.claim_outbox("publisher-test")

    assert job is not None
    assert job["status"] == "queued"
    assert service._tasks == set()
    assert len(outbox) == 1
    assert outbox[0]["diagnosis_id"] == diagnosis_id
    assert outbox[0]["queue_name"] == config.celery_critical_queue


async def test_execution_lease_prevents_duplicate_worker():
    repository = InMemoryDiagnosisRepository()
    diagnosis_id = "diag-lease-test"
    await repository.create_or_get_job_and_enqueue(
        {
            "diagnosis_id": diagnosis_id,
            "event_key": "lease-test",
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert_name": "LeaseTest",
            "severity": None,
            "alert": {"session_id": "lease-test"},
        },
        config.celery_default_queue,
    )

    first = await repository.claim_job(diagnosis_id, "task-1", "worker-1")
    duplicate = await repository.claim_job(diagnosis_id, "task-1", "worker-2")

    assert first == "claimed"
    assert duplicate == "busy"
    job = await repository.get_job(diagnosis_id)
    assert job is not None
    assert job["attempt_count"] == 1
    assert job["worker_name"] == "worker-1"


async def test_outbox_failure_is_requeued():
    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job_and_enqueue(
        {
            "diagnosis_id": "diag-outbox-test",
            "event_key": "outbox-test",
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert_name": "OutboxTest",
            "severity": None,
            "alert": {"session_id": "outbox-test"},
        },
        config.celery_default_queue,
    )
    event = (await repository.claim_outbox("publisher-test"))[0]
    await repository.mark_outbox_failed(event["id"], "rabbitmq unavailable")

    assert await repository.claim_outbox("publisher-test") == []
