"""Regression coverage for untrusted alert identity and bounded diagnosis reads."""

import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from app.models.alerts import AlertmanagerAlert, AlertmanagerWebhook
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.alert_diagnosis_service import AlertDiagnosisService


def alert(**changes):
    return {"status": "firing", "fingerprint": "boundary",
            "startsAt": "2026-10-23T00:00:00Z", "endsAt": "",
            "labels": {"alertname": "HighErrors", "service": "checkout", "region": "east"},
            **changes}


async def enqueue(service, **changes):
    return (await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [alert(**changes)]})))[0]


@pytest.mark.parametrize("changes", [
    {"startsAt": "bad"}, {"startsAt": "2026-10-23T00:00:00"},
    {"startsAt": ""}, {"endsAt": "bad"},
    {"status": "resolved", "endsAt": "2026-10-22T23:59:00Z"},
    {"fingerprint": "x" * 256},
    {"labels": {"alertname": "x" * 256}},
    {"labels": {"service": "x" * 256}},
    {"labels": {"severity": "x" * 65}},
    {"startsAt": "2026-10-23T00:00:00+05:60"},
    {"startsAt": "2026-10-23T00:00:00+05:99"},
    {"startsAt": "2026-10-23T00:00:00+24:00"},
    {"startsAt": "2026-10-23 00:00:00Z"},
])
def test_invalid_alert_identity_is_rejected(changes):
    with pytest.raises(ValidationError):
        AlertmanagerAlert.model_validate(alert(**changes))


def test_alertmanager_zero_end_is_valid_for_firing():
    assert AlertmanagerAlert.model_validate(alert(endsAt="0001-01-01T00:00:00Z")).status == "firing"


async def test_invalid_batch_cannot_partially_enqueue():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    payload = AlertmanagerWebhook.model_construct(alerts=[
        AlertmanagerAlert.model_validate(alert()),
        AlertmanagerAlert.model_construct(**{"status": "resolved", "starts_at": "bad", "ends_at": "bad"}),
    ])
    with pytest.raises(ValidationError):
        await service.handle_webhook(payload)
    assert await repo.list_jobs() == []
    assert await repo.claim_outbox("test") == []


async def test_stale_resolve_leaves_newer_period_firing():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    old = await enqueue(service)
    new = await enqueue(service, startsAt="2026-10-23T01:00:00Z")
    await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [alert(
        status="resolved", endsAt="2026-10-23T00:10:00Z")]}))
    assert (await repo.get_job(old))["alert_status"] == "resolved"
    assert (await repo.get_job(new))["alert_status"] == "firing"


async def test_fingerprintless_resolve_requires_all_labels():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    east = await enqueue(service, fingerprint="")
    west_labels = {"alertname": "HighErrors", "service": "checkout", "region": "west"}
    west = await enqueue(service, fingerprint="", labels=west_labels)
    await service.handle_webhook(AlertmanagerWebhook.model_validate({"alerts": [alert(
        fingerprint="", status="resolved", endsAt="2026-10-23T00:10:00Z")]}))
    assert (await repo.get_job(east))["alert_status"] == "resolved"
    assert (await repo.get_job(west))["alert_status"] == "firing"


async def test_equivalent_timestamps_dedupe_without_rewriting_legacy_id():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    legacy_key = "boundary:2026-10-23T08:00:00+08:00"
    legacy_id = "diag-" + hashlib.sha256(legacy_key.encode()).hexdigest()[:16]
    await repo.create_or_get_job_and_enqueue({
        "diagnosis_id": legacy_id, "event_key": legacy_key, "fingerprint": "boundary",
        "trigger": "alertmanager", "status": "queued", "alert_status": "firing",
        "alert": alert(startsAt="2026-10-23T08:00:00+08:00"),
    }, "test")
    assert await enqueue(service) == legacy_id
    assert await enqueue(service, startsAt="2026-10-23T00:00:00.000000+00:00") == legacy_id
    assert len(await repo.claim_outbox("test")) == 1


async def test_distinct_nanosecond_periods_remain_distinct():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    first = await enqueue(service, startsAt="2026-10-23T00:00:00.123456001Z")
    second = await enqueue(service, startsAt="2026-10-23T00:00:00.123456002Z")
    assert first != second
    assert await enqueue(service, startsAt="2026-10-23T08:00:00.123456001+08:00") == first


def test_resolved_nanosecond_end_before_start_is_rejected():
    with pytest.raises(ValidationError):
        AlertmanagerAlert.model_validate(alert(status="resolved",
            startsAt="2026-10-23T00:00:00.123456002Z", endsAt="2026-10-23T00:00:00.123456001Z"))


async def test_paginated_summaries_and_incremental_terminal_stream():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    ids = [await service.enqueue_manual(str(i)) for i in range(3)]
    job_id = ids[-1]
    for index in range(205):
        await repo.record_event(job_id, {"type": "status", "index": index})
    await repo.update_job(job_id, status="completed")
    summaries = await service.list_jobs(limit=2, offset=1)
    assert len(summaries) == 2
    assert all(not {"events", "evidence", "steps", "alert", "report"} & item.keys() for item in summaries)
    first = await repo.get_job_stream(job_id, limit=100)
    assert len(first["evidence"]) == 100
    second = await repo.get_job_stream(job_id, after_id=first["evidence"][-1]["id"], limit=100)
    assert len(second["evidence"]) == 100
    events = [item async for item in service.stream_job(job_id)]
    assert [item["index"] for item in events if "index" in item] == list(range(205))
    assert events[-1]["type"] == "complete"


async def test_budget_atomic_and_durable_across_retry():
    repo = InMemoryDiagnosisRepository()
    service = AlertDiagnosisService(repo, inline_worker=False)
    job_id = await service.enqueue_manual("budget")
    await repo.claim_job(job_id, "task", "worker", execution_token="first")
    async def reserve(token="first", size=3):
        return await repo.reserve_model_budget(job_id, execution_token=token, input_bytes=size,
                                              max_calls=3, max_input_bytes=8)
    assert sorted(await asyncio.gather(*(reserve() for _ in range(3)))) == [False, True, True]
    await repo.prepare_retry(job_id, "retry", execution_token="first")
    await repo.claim_job(job_id, "task", "worker", execution_token="second")
    assert not await reserve("first", 1)
    assert await reserve("second", 2)
    assert not await reserve("second", 0)
    job = await repo.get_job(job_id)
    assert (job["model_calls"], job["model_input_bytes"]) == (3, 8)
