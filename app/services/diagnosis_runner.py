"""Shared diagnosis execution logic used by Celery and inline tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from app.config import config
from app.core.metrics import AIOPS_ACTIVE, AIOPS_DIAGNOSES
from app.models.alerts import AlertmanagerAlert
from app.repositories.diagnosis import ClaimResult, DiagnosisRepository
from app.services.aiops_service import aiops_service
from app.services.service_catalog import service_catalog


class DiagnosisBusyError(RuntimeError):
    """Another worker currently owns a valid execution lease."""


class DiagnosisNotFoundError(RuntimeError):
    """The broker message references a missing diagnosis."""


def build_alert_task(alert: AlertmanagerAlert, service_name: str, service: dict[str, Any]) -> str:
    payload = {
        "alertname": alert.labels.get("alertname", "unknown"),
        "severity": alert.labels.get("severity", "unknown"),
        "service": service_name,
        "instance": alert.labels.get("instance", ""),
        "starts_at": alert.starts_at,
        "summary": alert.annotations.get("summary", ""),
        "description": alert.annotations.get("description", ""),
        "prometheus_job": service.get("prometheus_job", service_name),
        "metrics": service.get("metrics", {}),
        "cls": service.get("cls", {}),
        "owner": service.get("owner", "unknown"),
    }
    return (
        "对下列 Alertmanager firing 告警执行真实诊断。\n"
        f"告警与服务映射：{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "要求：\n"
        "1. 先用 Prometheus 告警工具确认告警仍在 firing。\n"
        "2. 只查询该服务及告警时间窗口的真实指标和日志；优先使用上述 metrics 和 CLS 映射。\n"
        "3. 每个结论都要写明证据来源、查询时间范围和关键数值。\n"
        "4. 工具无法获取数据时必须明确写明证据缺失，禁止编造。\n"
        "5. 输出 Markdown 报告，包含告警摘要、指标证据、日志证据、根因判断、处理建议和恢复验证项。"
    )


class DiagnosisRunner:
    def __init__(self, repository: DiagnosisRepository) -> None:
        self.repository = repository

    async def run(self, diagnosis_id: str, celery_task_id: str, worker_name: str) -> str:
        claim: ClaimResult = await self.repository.claim_job(
            diagnosis_id, celery_task_id, worker_name
        )
        if claim == "completed":
            return "already_completed"
        if claim == "busy":
            raise DiagnosisBusyError(f"diagnosis {diagnosis_id} has an active lease")
        if claim == "missing":
            raise DiagnosisNotFoundError(f"diagnosis {diagnosis_id} does not exist")

        job = await self.repository.get_job(diagnosis_id)
        if job is None:  # pragma: no cover - guarded by the claim transaction
            raise DiagnosisNotFoundError(diagnosis_id)

        trigger = str(job.get("trigger", "unknown"))
        AIOPS_DIAGNOSES.labels(trigger=trigger, status="started").inc()
        AIOPS_ACTIVE.labels(trigger=trigger).inc()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(diagnosis_id, celery_task_id),
            name=f"diagnosis-heartbeat-{diagnosis_id}",
        )
        error_event_seen = False
        try:
            if trigger == "manual":
                session_id = str(job.get("alert", {}).get("session_id", diagnosis_id))
                events = aiops_service.diagnose(session_id=session_id)
            else:
                alert = AlertmanagerAlert.model_validate(job["alert"])
                service_name, service = service_catalog.resolve(alert.labels)
                prompt = build_alert_task(alert, service_name, service)
                events = aiops_service.execute(prompt, session_id=diagnosis_id)

            async for event in events:
                durable_event = {
                    **event,
                    "diagnosis_id": diagnosis_id,
                    "attempt": job["attempt_count"],
                    "worker": worker_name,
                }
                await self.repository.record_event(diagnosis_id, durable_event)
                if event.get("type") == "error":
                    error_event_seen = True
                    raise RuntimeError(str(event.get("message", "diagnosis failed")))

            # A completed job must never leave planned steps behind. The report event
            # normally carries precise skip reasons; this is the final invariant guard.
            await self.repository.skip_remaining_steps(
                diagnosis_id, "诊断工作流已结束，该步骤未执行"
            )
            await self.repository.update_job(diagnosis_id, status="completed", error=None)
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="completed").inc()
            return "completed"
        except asyncio.CancelledError:
            await self.repository.record_event(
                diagnosis_id,
                {
                    "type": "error",
                    "stage": "interrupted",
                    "message": "diagnosis worker was cancelled",
                    "diagnosis_id": diagnosis_id,
                },
            )
            await self.repository.update_job(
                diagnosis_id, status="interrupted", error="diagnosis worker was cancelled"
            )
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="interrupted").inc()
            raise
        except Exception as exc:
            if not error_event_seen:
                await self.repository.record_event(
                    diagnosis_id,
                    {
                        "type": "error",
                        "stage": "exception",
                        "message": str(exc),
                        "diagnosis_id": diagnosis_id,
                    },
                )
            await self.repository.update_job(diagnosis_id, status="failed", error=str(exc))
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="failed").inc()
            logger.exception("Diagnosis execution failed: {}", diagnosis_id)
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            AIOPS_ACTIVE.labels(trigger=trigger).dec()

    async def _heartbeat_loop(self, diagnosis_id: str, celery_task_id: str) -> None:
        while True:
            await asyncio.sleep(config.diagnosis_heartbeat_seconds)
            await self.repository.heartbeat(diagnosis_id, celery_task_id)
