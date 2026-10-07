import asyncio

import pytest

from app.models.alerts import AlertmanagerWebhook
from app.services.aiops_service import aiops_service
from app.services.alert_diagnosis_service import AlertDiagnosisService


def _payload(status: str = "firing") -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(
        {
            "receiver": "oncall-agent",
            "status": status,
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": "HighHTTPErrorRate",
                        "severity": "critical",
                        "service": "ai-oncall-agent",
                        "job": "ai-oncall-agent",
                    },
                    "annotations": {
                        "summary": "error rate high",
                        "description": "5xx ratio exceeded 5%",
                    },
                    "startsAt": "2026-10-07T10:00:00+08:00",
                    "endsAt": "2026-10-07T10:10:00+08:00" if status == "resolved" else "",
                    "fingerprint": "abc123",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_firing_alert_creates_and_completes_diagnosis(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_execute(prompt: str, session_id: str):
        captured["prompt"] = prompt
        captured["session_id"] = session_id
        yield {"type": "report", "report": "# evidence-backed report"}
        yield {"type": "complete", "response": "# evidence-backed report"}

    monkeypatch.setattr(aiops_service, "execute", fake_execute)
    service = AlertDiagnosisService()
    diagnosis_ids = await service.handle_webhook(_payload())
    tasks = list(service._tasks)
    await asyncio.gather(*tasks)

    assert len(diagnosis_ids) == 1
    job = await service.get_job(diagnosis_ids[0])
    assert job is not None
    assert job["status"] == "completed"
    assert job["report"] == "# evidence-backed report"
    assert "HighHTTPErrorRate" in captured["prompt"]
    assert "Prometheus" in captured["prompt"]

    duplicate_ids = await service.handle_webhook(_payload())
    assert duplicate_ids == diagnosis_ids


@pytest.mark.asyncio
async def test_resolved_notification_updates_alert_state(monkeypatch):
    async def fake_execute(prompt: str, session_id: str):
        yield {"type": "complete", "response": "done"}

    monkeypatch.setattr(aiops_service, "execute", fake_execute)
    service = AlertDiagnosisService()
    diagnosis_id = (await service.handle_webhook(_payload()))[0]
    await asyncio.gather(*list(service._tasks))

    await service.handle_webhook(_payload("resolved"))
    job = await service.get_job(diagnosis_id)
    assert job is not None
    assert job["alert_status"] == "resolved"
    assert job["resolved_at"] == "2026-10-07T10:10:00+08:00"
