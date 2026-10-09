import pytest

from app.api import health
from app.config import config
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.queue_observer import QueueObserver


async def test_queue_observer_collects_database_and_broker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job_and_enqueue(
        {
            "diagnosis_id": "diag-observer",
            "event_key": "observer",
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert_name": "ObserverTest",
            "severity": None,
            "alert": {},
        },
        config.celery_default_queue,
    )
    observer = QueueObserver(repository)

    async def fake_queues():
        return {
            config.celery_critical_queue: {
                "messages_ready": 0,
                "messages_unacknowledged": 0,
                "consumers": 1,
            },
            config.celery_default_queue: {
                "messages_ready": 1,
                "messages_unacknowledged": 0,
                "consumers": 1,
            },
            config.celery_dead_letter_queue: {
                "messages_ready": 0,
                "messages_unacknowledged": 0,
                "consumers": 0,
            },
        }

    monkeypatch.setattr(observer, "rabbitmq_queues", fake_queues)
    snapshot = await observer.collect_once()

    assert snapshot["database"]["job_counts"] == {"queued": 1}
    assert snapshot["database"]["outbox_counts"] == {"pending": 1}
    assert snapshot["rabbitmq"][config.celery_default_queue]["consumers"] == 1


async def test_operational_metrics_track_dead_letters() -> None:
    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job(
        {
            "diagnosis_id": "diag-dead-observer",
            "event_key": "dead-observer",
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert_name": "DeadObserver",
            "severity": None,
            "alert": {},
        }
    )
    await repository.mark_dead_lettered("diag-dead-observer", "failed")

    metrics = await repository.get_operational_metrics()

    assert metrics["job_counts"] == {"dead_lettered": 1}


async def test_readiness_requires_authenticated_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.milvus_manager, "health_check", lambda: True)

    async def database_ok() -> bool:
        return True

    async def rabbitmq_ok():
        return {
            config.celery_critical_queue: {"consumers": 1},
            config.celery_default_queue: {"consumers": 1},
        }

    monkeypatch.setattr(health.database_manager, "health_check", database_ok)
    monkeypatch.setattr(health.queue_observer, "rabbitmq_queues", rabbitmq_ok)

    response = await health.readiness()

    assert response.status_code == 200
    assert b'"status":"ready"' in response.body
