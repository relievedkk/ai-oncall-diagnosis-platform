import importlib

import pytest

from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.aiops_service import AIOpsService, aiops_service
from app.services.diagnosis_runner import DiagnosisRunner


def remediation_payload():
    return {
        "action_type": "restart_instance",
        "service_name": "orders",
        "instance_id": "orders-1",
        "reason": "The instance is unhealthy.",
    }


def test_report_event_without_remediation_is_byte_for_byte_compatible():
    event = AIOpsService()._format_replanner_event(
        {
            "response": "# Existing report",
            "plan": [],
            "skipped_steps": [],
            "termination_reason": "sufficient_evidence",
        }
    )

    assert event == {
        "type": "report",
        "stage": "final_report",
        "message": "最终报告已生成",
        "report": "# Existing report",
        "skipped_steps": [],
        "termination_reason": "sufficient_evidence",
    }


def test_report_event_contains_one_valid_typed_remediation_summary():
    event = AIOpsService()._format_replanner_event(
        {
            "response": "# Evidence-backed report",
            "plan": [],
            "remediation": remediation_payload(),
        }
    )

    assert event["remediation"] == remediation_payload()
    assert set(event["remediation"]) == {
        "action_type",
        "service_name",
        "instance_id",
        "reason",
    }


def test_invalid_model_remediation_is_dropped_and_recorded_as_harness_error():
    event = AIOpsService()._format_replanner_event(
        {
            "response": "# Evidence-backed report",
            "plan": [],
            "remediation": {
                **remediation_payload(),
                "command": "kubectl delete pod orders-1",
            },
        }
    )

    assert "remediation" not in event
    assert event["harness_error"] == "invalid_remediation_proposal"
    assert "command" not in str(event)


@pytest.mark.asyncio
async def test_invalid_structured_remediation_preserves_valid_model_report(monkeypatch):
    replanner = importlib.import_module("app.agent.aiops.replanner")
    payload = {
        "response": "# Valid diagnosis report",
        "remediation": {
            **remediation_payload(),
            "command": "kubectl delete pod orders-1",
        },
    }

    class FakeResponseGenerator:
        async def ainvoke(self, _inputs):
            return replanner.Response.model_validate(payload)

    class FakePrompt:
        def __or__(self, _other):
            return FakeResponseGenerator()

    class FakeLLM:
        def with_structured_output(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(replanner, "response_prompt", FakePrompt())

    result = await replanner._generate_response({"input": "diagnose", "past_steps": []}, FakeLLM())

    assert result['diagnosis_result']['outcome'] == 'legacy_unverified'
    assert {key: value for key, value in result.items() if key != 'diagnosis_result'} == {
        "response": "# Valid diagnosis report",
        "harness_error": "invalid_remediation_proposal",
    }


class TrackingRepository(InMemoryDiagnosisRepository):
    def __init__(self, order):
        super().__init__()
        self.order = order

    async def record_event(self, diagnosis_id, event, **kwargs):
        self.order.append(f"record:{event.get('type')}")
        await super().record_event(diagnosis_id, event, **kwargs)


class RecordingRemediationService:
    def __init__(self, order, error=None):
        self.order = order
        self.error = error
        self.calls = []
        self.execution_tokens = []

    async def propose(self, diagnosis_id, intent, evidence, requested_by="agent", **kwargs):
        self.execution_tokens.append(kwargs.get("execution_token"))
        self.order.append("propose")
        self.calls.append((diagnosis_id, intent, evidence, requested_by))
        if self.error is not None:
            raise self.error
        return {"id": "action-1", "status": "proposed"}


async def create_manual_job(repository, diagnosis_id):
    await repository.create_or_get_job(
        {
            "diagnosis_id": diagnosis_id,
            "event_key": diagnosis_id,
            "trigger": "manual",
            "status": "queued",
            "alert_status": "not_applicable",
            "service": "orders",
            "alert": {"session_id": diagnosis_id},
        }
    )


async def remediation_event_stream(session_id):
    yield {
        "type": "report",
        "stage": "final_report",
        "report": "# Evidence-backed report",
        "remediation": remediation_payload(),
    }
    yield {
        "type": "remediation_proposed",
        "stage": "remediation_proposed",
        "message": "A bounded remediation was proposed for policy evaluation",
        "remediation": remediation_payload(),
    }
    yield {"type": "complete", "response": "# Evidence-backed report"}


@pytest.mark.asyncio
async def test_runner_records_report_before_submitting_remediation(monkeypatch):
    order = []
    repository = TrackingRepository(order)
    remediation = RecordingRemediationService(order)
    diagnosis_id = "diagnosis-remediation-order"
    await create_manual_job(repository, diagnosis_id)
    monkeypatch.setattr(aiops_service, "diagnose", remediation_event_stream)

    result = await DiagnosisRunner(repository, remediation=remediation).run(
        diagnosis_id,
        "task-1",
        "worker-1",
    )

    assert result == "completed"
    assert order.index("record:report") < order.index("propose")
    assert remediation.calls[0][0] == diagnosis_id
    assert remediation.calls[0][3] == "agent"
    assert remediation.execution_tokens[0] == (await repository.get_job(diagnosis_id))["execution_token"]


@pytest.mark.asyncio
async def test_runner_remediation_failure_does_not_change_completed_diagnosis(monkeypatch):
    order = []
    repository = TrackingRepository(order)
    remediation = RecordingRemediationService(order, RuntimeError("policy store down"))
    diagnosis_id = "diagnosis-remediation-failure"
    await create_manual_job(repository, diagnosis_id)
    monkeypatch.setattr(aiops_service, "diagnose", remediation_event_stream)

    result = await DiagnosisRunner(repository, remediation=remediation).run(
        diagnosis_id,
        "task-1",
        "worker-1",
    )

    assert result == "completed"
    job = await repository.get_job(diagnosis_id)
    assert job["status"] == "completed"
    harness_errors = [event for event in job["events"] if event.get("type") == "harness_error"]
    assert harness_errors[0]["stage"] == "remediation_proposal"
    assert "policy store down" in harness_errors[0]["message"]
