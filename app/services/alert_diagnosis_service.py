"""Turn Alertmanager notifications into asynchronous AIOps diagnoses."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.core.metrics import AIOPS_ACTIVE, AIOPS_DIAGNOSES
from app.models.alerts import AlertmanagerAlert, AlertmanagerWebhook
from app.services.aiops_service import aiops_service
from app.services.service_catalog import service_catalog


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class AlertDiagnosisService:
    """In-memory P0 job manager; persistent storage belongs to the next phase."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._event_index: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

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
                await self._mark_resolved(alert)
                continue

            event_key = self._event_key(alert)
            async with self._lock:
                existing = self._event_index.get(event_key)
                if existing:
                    diagnosis_ids.append(existing)
                    continue

                diagnosis_id = self._diagnosis_id(event_key)
                service_name, service = service_catalog.resolve(alert.labels)
                self._event_index[event_key] = diagnosis_id
                self._jobs[diagnosis_id] = {
                    "diagnosis_id": diagnosis_id,
                    "event_key": event_key,
                    "fingerprint": alert.fingerprint,
                    "status": "queued",
                    "alert_status": "firing",
                    "service": service_name,
                    "alert": alert.model_dump(by_alias=True),
                    "events": [],
                    "report": "",
                    "error": None,
                    "created_at": _utc_now(),
                    "updated_at": _utc_now(),
                }

            diagnosis_ids.append(diagnosis_id)
            task = asyncio.create_task(
                self._run_diagnosis(diagnosis_id, alert, service_name, service),
                name=f"alert-diagnosis-{diagnosis_id}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return diagnosis_ids

    async def _mark_resolved(self, alert: AlertmanagerAlert) -> None:
        async with self._lock:
            for job in self._jobs.values():
                if alert.fingerprint:
                    if job.get("fingerprint") != alert.fingerprint:
                        continue
                else:
                    job_labels = job.get("alert", {}).get("labels", {})
                    identity_keys = ("alertname", "service", "job", "instance")
                    if any(
                        alert.labels.get(key, "") != job_labels.get(key, "")
                        for key in identity_keys
                    ):
                        continue
                job["alert_status"] = "resolved"
                job["resolved_at"] = alert.ends_at or _utc_now()
                job["updated_at"] = _utc_now()

    async def _run_diagnosis(
        self,
        diagnosis_id: str,
        alert: AlertmanagerAlert,
        service_name: str,
        service: dict[str, Any],
    ) -> None:
        trigger = "alertmanager"
        AIOPS_DIAGNOSES.labels(trigger=trigger, status="started").inc()
        AIOPS_ACTIVE.labels(trigger=trigger).inc()
        await self._update(diagnosis_id, status="running")
        try:
            prompt = self.build_task(alert, service_name, service)
            events: list[dict[str, Any]] = []
            report = ""
            async for event in aiops_service.execute(prompt, session_id=diagnosis_id):
                events.append(event)
                if event.get("type") == "report":
                    report = str(event.get("report", ""))
                elif event.get("type") == "complete":
                    report = str(event.get("response", "")) or report
                elif event.get("type") == "error":
                    raise RuntimeError(str(event.get("message", "diagnosis failed")))
                await self._update(diagnosis_id, events=events[-100:], report=report)

            await self._update(diagnosis_id, status="completed", report=report)
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="completed").inc()
        except Exception as exc:
            logger.exception("Alert-triggered diagnosis failed: {}", diagnosis_id)
            await self._update(diagnosis_id, status="failed", error=str(exc))
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="failed").inc()
        finally:
            AIOPS_ACTIVE.labels(trigger=trigger).dec()

    async def _update(self, diagnosis_id: str, **values: Any) -> None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job:
                return
            job.update(values)
            job["updated_at"] = _utc_now()

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            return dict(job) if job else None

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._lock:
            jobs = [dict(job) for job in self._jobs.values()]
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)


alert_diagnosis_service = AlertDiagnosisService()
