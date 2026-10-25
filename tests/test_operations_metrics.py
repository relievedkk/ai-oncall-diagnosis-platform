from datetime import UTC, datetime

import pytest

from app.core import metrics
from app.services.queue_observer import QueueObserver


class Repo:
    async def get_operational_metrics(self):
        return {'job_counts': {}, 'recent_failure_count': 0, 'outbox_counts': {},
                'outbox_oldest_pending_seconds': 0, 'oldest_waiting_seconds': 123}


@pytest.mark.asyncio
async def test_collector_records_wait_age_and_last_success_not_a_stale_healthy_value():
    observer = QueueObserver(repository=Repo(), now_factory=lambda: datetime(2026, 10, 24, tzinfo=UTC))

    async def healthy():
        return {'diagnosis.default': {'messages_ready': 4, 'messages_unacknowledged': 0, 'consumers': 1}}

    observer.rabbitmq_queues = healthy
    await observer.collect_once()
    assert metrics.DIAGNOSIS_OLDEST_WAITING_SECONDS._value.get() == 123
    timestamp = metrics.QUEUE_LAST_SUCCESS_TIMESTAMP_SECONDS._value.get()
    assert timestamp == datetime(2026, 10, 24, tzinfo=UTC).timestamp()

    async def failed():
        raise ValueError('unavailable')

    observer.rabbitmq_queues = failed
    await observer.collect_once()
    assert metrics.QUEUE_OBSERVER_UP._value.get() == 0
    assert metrics.QUEUE_LAST_SUCCESS_TIMESTAMP_SECONDS._value.get() == timestamp


@pytest.mark.asyncio
async def test_operational_alerts_do_not_create_more_diagnosis_jobs():
    from app.models.alerts import AlertmanagerWebhook
    from app.services.alert_diagnosis_service import AlertDiagnosisService

    service = AlertDiagnosisService(inline_worker=False)
    payload = AlertmanagerWebhook.model_validate({'status': 'firing', 'alerts': [{
        'status': 'firing', 'labels': {'alertname': 'DiagnosisQueueBacklog', 'oncall_route': 'operations'},
        'startsAt': '2026-10-24T00:00:00Z', 'fingerprint': 'ops',
    }]})
    assert await service.handle_webhook(payload) == []
    assert await service.list_jobs() == []
