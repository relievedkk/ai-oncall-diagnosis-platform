"""Persisted diagnosis orchestration for Alertmanager and manual runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger

from app.core.metrics import AIOPS_ACTIVE, AIOPS_DIAGNOSES
from app.models.alerts import AlertmanagerAlert, AlertmanagerWebhook
from app.repositories.diagnosis import (
    DiagnosisRepository,
    InMemoryDiagnosisRepository,
    diagnosis_repository,
)
from app.services.aiops_service import aiops_service
from app.services.service_catalog import service_catalog


class AlertDiagnosisService:
    """Create diagnoses and durably record their lifecycle and workflow events."""

    def __init__(self, repository: DiagnosisRepository | None = None) -> None:
        # New instances are isolated test-friendly services. The application singleton
        # below explicitly receives the PostgreSQL repository.
        self.repository = repository or InMemoryDiagnosisRepository()
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def _event_key(alert: AlertmanagerAlert) -> str:
        if alert.fingerprint:
            return f"{alert.fingerprint}:{alert.starts_at}"
        canonical = json.dumps(
            {"labels": alert.labels, "startsAt": alert.starts_at},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _diagnosis_id(event_key: str) -> str:
        return f"diag-{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def build_task(alert: AlertmanagerAlert, service_name: str, service: dict[str, Any]) -> str:
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

    async def handle_webhook(self, payload: AlertmanagerWebhook) -> list[str]:
        diagnosis_ids: list[str] = []
        for alert in payload.alerts:
            if alert.status == "resolved":
                await self.repository.mark_resolved(alert)
                continue

            event_key = self._event_key(alert)
            diagnosis_id = self._diagnosis_id(event_key)
            service_name, service = service_catalog.resolve(alert.labels)
            job, created = await self.repository.create_or_get_job(
                {
                    "diagnosis_id": diagnosis_id,
                    "event_key": event_key,
                    "fingerprint": alert.fingerprint,
                    "trigger": "alertmanager",
                    "status": "queued",
                    "alert_status": "firing",
                    "service": service_name,
                    "alert_name": alert.labels.get("alertname"),
                    "severity": alert.labels.get("severity"),
                    "alert": alert.model_dump(by_alias=True),
                }
            )
            diagnosis_ids.append(diagnosis_id)

            should_run = created
            if not created and job["status"] == "interrupted":
                await self.repository.reset_interrupted(diagnosis_id)
                should_run = True
            if not should_run:
                continue

            task = asyncio.create_task(
                self._run_alert_diagnosis(diagnosis_id, alert, service_name, service),
                name=f"alert-diagnosis-{diagnosis_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return diagnosis_ids

    async def _run_alert_diagnosis(
        self,
        diagnosis_id: str,
        alert: AlertmanagerAlert,
        service_name: str,
        service: dict[str, Any],
    ) -> None:
        trigger = "alertmanager"
        AIOPS_DIAGNOSES.labels(trigger=trigger, status="started").inc()
        AIOPS_ACTIVE.labels(trigger=trigger).inc()
        await self.repository.update_job(diagnosis_id, status="running", error=None)
        try:
            prompt = self.build_task(alert, service_name, service)
            async for event in aiops_service.execute(prompt, session_id=diagnosis_id):
                await self.repository.record_event(diagnosis_id, event)
                if event.get("type") == "error":
                    raise RuntimeError(str(event.get("message", "diagnosis failed")))

            await self.repository.update_job(diagnosis_id, status="completed", error=None)
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="completed").inc()
        except asyncio.CancelledError:
            await self.repository.record_event(
                diagnosis_id,
                {
                    "type": "error",
                    "stage": "interrupted",
                    "message": "diagnosis task was cancelled",
                },
            )
            await self.repository.update_job(
                diagnosis_id, status="interrupted", error="diagnosis task was cancelled"
            )
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="interrupted").inc()
            raise
        except Exception as exc:
            logger.exception("Alert-triggered diagnosis failed: {}", diagnosis_id)
            await self.repository.record_event(
                diagnosis_id,
                {"type": "error", "stage": "exception", "message": str(exc)},
            )
            await self.repository.update_job(diagnosis_id, status="failed", error=str(exc))
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="failed").inc()
        finally:
            AIOPS_ACTIVE.labels(trigger=trigger).dec()

    async def diagnose_manual(self, session_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Run the existing SSE diagnosis while persisting the same durable artifacts."""
        diagnosis_id = f"diag-manual-{uuid.uuid4().hex[:16]}"
        await self.repository.create_or_get_job(
            {
                "diagnosis_id": diagnosis_id,
                "event_key": f"manual:{diagnosis_id}",
                "fingerprint": None,
                "trigger": "manual",
                "status": "queued",
                "alert_status": "not_applicable",
                "service": "manual",
                "alert_name": "ManualDiagnosis",
                "severity": None,
                "alert": {"session_id": session_id, "source": "manual"},
            }
        )
        created_event = {
            "type": "status",
            "stage": "job_created",
            "message": "诊断任务已创建",
            "diagnosis_id": diagnosis_id,
        }
        await self.repository.record_event(diagnosis_id, created_event)
        yield created_event

        trigger = "manual"
        AIOPS_DIAGNOSES.labels(trigger=trigger, status="started").inc()
        AIOPS_ACTIVE.labels(trigger=trigger).inc()
        await self.repository.update_job(diagnosis_id, status="running")
        terminal_status = "completed"
        try:
            async for event in aiops_service.diagnose(session_id=session_id):
                event = {**event, "diagnosis_id": diagnosis_id}
                await self.repository.record_event(diagnosis_id, event)
                if event.get("type") == "error":
                    terminal_status = "failed"
                    await self.repository.update_job(
                        diagnosis_id, status="failed", error=str(event.get("message", "failed"))
                    )
                yield event
            if terminal_status == "completed":
                await self.repository.update_job(diagnosis_id, status="completed", error=None)
            AIOPS_DIAGNOSES.labels(trigger=trigger, status=terminal_status).inc()
        except asyncio.CancelledError:
            await self.repository.record_event(
                diagnosis_id,
                {"type": "error", "stage": "interrupted", "message": "client disconnected"},
            )
            await self.repository.update_job(
                diagnosis_id, status="interrupted", error="client disconnected"
            )
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="interrupted").inc()
            raise
        except Exception as exc:
            error_event = {
                "type": "error",
                "stage": "exception",
                "message": f"诊断异常: {exc}",
                "diagnosis_id": diagnosis_id,
            }
            await self.repository.record_event(diagnosis_id, error_event)
            await self.repository.update_job(diagnosis_id, status="failed", error=str(exc))
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="failed").inc()
            logger.exception("Manual diagnosis failed: {}", diagnosis_id)
            yield error_event
        finally:
            AIOPS_ACTIVE.labels(trigger=trigger).dec()

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        return await self.repository.get_job(diagnosis_id)

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self.repository.list_jobs()

    async def shutdown(self) -> None:
        """Cancel in-flight jobs before the database pool is disposed."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


alert_diagnosis_service = AlertDiagnosisService(repository=diagnosis_repository)
