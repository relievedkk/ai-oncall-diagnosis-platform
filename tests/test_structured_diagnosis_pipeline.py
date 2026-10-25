import importlib

import pytest
from langchain_core.runnables import RunnableLambda

from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.aiops_service import AIOpsService
from app.services.alert_diagnosis_service import AlertDiagnosisService


class Model:
    def __init__(self, payload):
        self.payload = payload

    def with_structured_output(self, *args, **kwargs):
        return RunnableLambda(lambda _: self.payload)


async def test_invalid_reference_downgrades_report_and_discards_action():
    module = importlib.import_module("app.agent.aiops.replanner")
    result = await module._generate_response(
        {"input": "orders incident", "task_scope": "task-a", "evidence": [], "past_steps": []},
        Model(
            {
                "response": "confirmed",
                "diagnosis_result": {
                    "outcome": "diagnosed",
                    "summary": "database down",
                    "observations": [{"text": "db down", "evidence_refs": ["other-task"]}],
                    "hypotheses": [
                        {
                            "description": "db down",
                            "supporting_refs": ["other-task"],
                            "status": "supported",
                        }
                    ],
                },
                "remediation": {
                    "action_type": "restart_instance",
                    "service_name": "orders",
                    "instance_id": "o1",
                    "reason": "down",
                },
            }
        ),
    )
    assert result["diagnosis_result"]["outcome"] == "insufficient_evidence"
    assert result["diagnosis_result"]["validation_issues"]
    assert "remediation" not in result


async def test_exhausted_budget_renders_partial_without_another_model_call():
    module = importlib.import_module("app.agent.aiops.replanner")

    class ForbiddenModel:
        def with_structured_output(self, *args, **kwargs):
            raise AssertionError("budget exhausted must not call provider")

    result = await module._generate_response_and_close_plan(
        {"input": "incident", "task_scope": "budget", "past_steps": [], "plan": ["query logs"]},
        ForbiddenModel(),
        "max_steps_reached",
    )
    assert result["diagnosis_result"]["outcome"] == "budget_exhausted"
    assert result["skipped_steps"] == ["query logs"]


async def test_report_result_survives_repository_and_terminal_reconstruction():
    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job(
        {"diagnosis_id": "structured", "event_key": "structured", "status": "queued"}
    )
    structured = {
        "schema_version": 1,
        "outcome": "insufficient_evidence",
        "summary": "missing logs",
    }
    await repository.record_event(
        "structured", {"type": "report", "report": "partial", "diagnosis_result": structured}
    )
    await repository.update_job("structured", status="completed")
    job = await repository.get_job("structured")
    assert job["diagnosis_result"] == structured
    page = await repository.get_job_stream("structured", after_id=100)
    assert page["diagnosis_result"] == structured
    service = AlertDiagnosisService(repository=repository, inline_worker=False)
    events = [event async for event in service.stream_job("structured", after_id=100)]
    assert events[-1]["type"] == "complete"
    assert events[-1]["diagnosis_result"] == structured


def test_format_report_carries_result_without_changing_legacy_shape():
    service = AIOpsService()
    structured = {"outcome": "insufficient_evidence"}
    event = service._format_replanner_event({"response": "partial", "diagnosis_result": structured})
    assert event["diagnosis_result"] == structured
    assert "diagnosis_result" not in service._format_replanner_event({"response": "old"})


async def test_runner_rejects_remediation_even_if_evaluation_supplies_event():
    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.services.diagnosis_runner import DiagnosisRunner

    class Forbidden:
        async def propose(self, *args, **kwargs):
            raise AssertionError("evaluation must never submit remediation")

    runner = DiagnosisRunner(InMemoryDiagnosisRepository(), remediation=Forbidden())
    with diagnosis_runtime(DiagnosisRuntime(context="evaluation")):
        await runner._submit_remediation(
            "case",
            {
                "remediation": {
                    "action_type": "restart_instance",
                    "service_name": "orders",
                    "instance_id": "o1",
                    "reason": "down",
                }
            },
            "token",
        )


async def test_structured_partial_error_is_saved_and_replaced_by_new_attempt():
    repository = InMemoryDiagnosisRepository()
    await repository.create_or_get_job(
        {"diagnosis_id": "retry-result", "event_key": "retry-result", "status": "queued"}
    )
    await repository.record_event(
        "retry-result",
        {
            "type": "error",
            "response": "partial",
            "diagnosis_result": {"outcome": "dependency_failed"},
        },
    )
    assert (await repository.get_job("retry-result"))["diagnosis_result"][
        "outcome"
    ] == "dependency_failed"
    await repository.claim_job("retry-result", "task", "worker", execution_token="new")
    assert (await repository.get_job("retry-result"))["diagnosis_result"] is None


async def test_failed_dependency_cannot_be_promoted_to_diagnosed_by_model():
    module = importlib.import_module("app.agent.aiops.replanner")
    result = await module._generate_response(
        {
            "input": "orders",
            "task_scope": "failed",
            "execution_errors": ["logs unavailable"],
            "past_steps": [("metrics", "CPU 95%")],
            "evidence": [
                {
                    "task_scope": "failed",
                    "evidence_ref": "metric",
                    "evidence_type": "metric",
                    "success": True,
                    "result": {"success": True, "value": 95},
                }
            ],
        },
        Model(
            {
                "response": "confirmed",
                "diagnosis_result": {
                    "outcome": "diagnosed",
                    "summary": "CPU high",
                    "observations": [{"text": "CPU high", "evidence_refs": ["metric"]}],
                    "hypotheses": [
                        {
                            "description": "load",
                            "supporting_refs": ["metric"],
                            "status": "supported",
                        }
                    ],
                },
            }
        ),
    )
    assert result["diagnosis_result"]["outcome"] == "dependency_failed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"success": False},
        {"task_scope": "foreign"},
        {"result": {"success": True, "is_demo": True, "alerts": [], "total": 0}},
        {"result": {"success": True, "service_name": "billing", "alerts": [], "total": 0}},
        {"result": {"success": True, "provenance": "synthetic_fixture", "alerts": [], "total": 0}},
    ],
)
def test_invalid_empty_scan_cannot_end_production_diagnosis(overrides):
    module = importlib.import_module("app.agent.aiops.replanner")
    evidence = {
        "source": "query_prometheus_alerts",
        "success": True,
        "task_scope": "task",
        "result": {"success": True, "alerts": [], "total": 0},
    }
    evidence.update(overrides)
    assert (
        module._find_empty_alert_scan(
            [evidence], task_scope="task", target_service="checkout", context="production"
        )
        is None
    )
