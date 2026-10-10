import json

import pytest

from app.agent.aiops.replanner import replanner
from app.repositories.diagnosis import InMemoryDiagnosisRepository
from app.services.aiops_service import AIOpsService
from app.services.alert_diagnosis_service import AlertDiagnosisService


@pytest.mark.asyncio
async def test_empty_alert_scan_ends_with_evidence_bounded_report():
    remaining_steps = ["查询服务指标", "查询错误日志"]
    result = await replanner(
        {
            "input": "诊断当前系统是否存在告警",
            "plan": remaining_steps,
            "past_steps": [("查询当前告警", "Prometheus 返回 0 条告警")],
            "evidence": [
                {
                    "evidence_type": "alert",
                    "source": "query_prometheus_alerts",
                    "query": {},
                    "result": json.dumps(
                        {
                            "success": True,
                            "alerts": [],
                            "state_counts": {},
                            "total": 0,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "response": "",
        }
    )

    assert result["plan"] == []
    assert result["skipped_steps"] == remaining_steps
    assert result["termination_reason"] == "no_active_alerts"
    assert "当前活跃告警数**：0" in result["response"]
    assert "不能据此断言所有服务正常" in result["response"]
    assert "所有监控指标均在正常范围" not in result["response"]
    assert "风险较低" not in result["response"]


def test_no_alert_report_event_exposes_skipped_steps():
    event = AIOpsService()._format_replanner_event(
        {
            "response": "# 告警扫描报告",
            "plan": [],
            "skipped_steps": ["查询服务指标"],
            "termination_reason": "no_active_alerts",
        }
    )

    assert event["type"] == "report"
    assert event["skipped_steps"] == ["查询服务指标"]
    assert event["termination_reason"] == "no_active_alerts"
    assert "范围受限" in event["message"]


@pytest.mark.asyncio
async def test_report_marks_unexecuted_steps_as_skipped():
    repository = InMemoryDiagnosisRepository()
    diagnosis_id = "diag-no-alert"
    await repository.create_or_get_job(
        {
            "diagnosis_id": diagnosis_id,
            "event_key": diagnosis_id,
            "trigger": "manual",
            "status": "running",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert": {},
        }
    )
    await repository.record_event(
        diagnosis_id,
        {"type": "plan", "stage": "plan_created", "plan": ["查询告警", "查询指标"]},
    )
    await repository.record_event(
        diagnosis_id,
        {
            "type": "step_complete",
            "stage": "step_executed",
            "current_step": "查询告警",
            "result": "0 alerts",
        },
    )
    await repository.record_event(
        diagnosis_id,
        {
            "type": "report",
            "stage": "final_report",
            "report": "# 告警扫描报告",
            "skipped_steps": ["查询指标"],
            "termination_reason": "no_active_alerts",
        },
    )

    job = await repository.get_job(diagnosis_id)
    assert job is not None
    assert [step["status"] for step in job["steps"]] == ["completed", "skipped"]
    assert "不适用于当前场景" in job["steps"][1]["result"]


@pytest.mark.asyncio
async def test_stream_wraps_tool_evidence_in_readable_event():
    repository = InMemoryDiagnosisRepository()
    diagnosis_id = "diag-stream-evidence"
    await repository.create_or_get_job(
        {
            "diagnosis_id": diagnosis_id,
            "event_key": diagnosis_id,
            "trigger": "manual",
            "status": "running",
            "alert_status": "not_applicable",
            "service": "manual",
            "alert": {},
        }
    )
    await repository.record_event(
        diagnosis_id,
        {
            "type": "step_complete",
            "stage": "step_executed",
            "current_step": "查询告警",
            "result": "0 alerts",
            "tool_evidence": [
                {
                    "evidence_type": "alert",
                    "source": "query_prometheus_alerts",
                    "query": {},
                    "result": json.dumps(
                        {"success": True, "alerts": [], "state_counts": {}, "total": 0}
                    ),
                }
            ],
        },
    )
    await repository.update_job(diagnosis_id, status="completed")
    service = AlertDiagnosisService(repository=repository, inline_worker=False)

    events = [event async for event in service.stream_job(diagnosis_id)]
    step_event = next(event for event in events if event["type"] == "step_complete")
    evidence_event = next(event for event in events if event["type"] == "evidence")

    assert "result" not in step_event
    assert "tool_evidence" not in step_event
    assert evidence_event["summary"] == "Prometheus 当前活跃告警：0 条"
    assert "result" not in evidence_event


def test_frontend_handles_evidence_and_skipped_steps_without_raw_json():
    source = open("static/app.js", encoding="utf-8").read()
    assert "sseMessage.type === 'evidence'" in source
    assert "sseMessage.skipped_steps" in source
