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
        yield {"type": "plan", "stage": "plan_created", "plan": ["query metrics"]}
        yield {
            "type": "step_complete",
            "stage": "step_executed",
            "current_step": "query metrics",
            "result": "http_5xx_rate=0.08",
            "tool_evidence": [
                {
                    "evidence_type": "metric",
                    "source": "query_service_metric",
                    "query": {"metric": "http_5xx_rate"},
                    "result": "0.08",
                }
            ],
        }
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
    assert job["steps"][0]["status"] == "completed"
    assert job["steps"][0]["result"] == "http_5xx_rate=0.08"
    assert [item["evidence_type"] for item in job["evidence"]] == [
        "plan",
        "step_complete",
        "metric",
        "report",
        "complete",
    ]
    assert "HighHTTPErrorRate" in captured["prompt"]
    assert "Prometheus" in captured["prompt"]
    assert job["evidence"][2]["source"] == "query_service_metric"

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


@pytest.mark.asyncio
async def test_manual_diagnosis_is_persisted(monkeypatch):
    async def fake_diagnose(session_id: str):
        yield {"type": "plan", "stage": "plan_created", "plan": ["inspect logs"]}
        yield {
            "type": "step_complete",
            "stage": "step_executed",
            "current_step": "inspect logs",
            "result": "timeout_count=12",
        }
        yield {
            "type": "complete",
            "stage": "diagnosis_complete",
            "diagnosis": {"status": "completed", "report": "# manual report"},
        }

    monkeypatch.setattr(aiops_service, "diagnose", fake_diagnose)
    service = AlertDiagnosisService()
    events = [event async for event in service.diagnose_manual("manual-session")]

    diagnosis_id = events[0]["diagnosis_id"]
    job = await service.get_job(diagnosis_id)
    assert job is not None
    assert job["trigger"] == "manual"
    assert job["status"] == "completed"
    assert job["steps"][0]["result"] == "timeout_count=12"
    assert job["report"] == "# manual report"
