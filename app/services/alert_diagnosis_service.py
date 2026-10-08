"""Queue-facing diagnosis service for Alertmanager and manual requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from app.config import config
from app.models.alerts import AlertmanagerAlert, AlertmanagerWebhook
from app.repositories.diagnosis import (
    DiagnosisRepository,
    InMemoryDiagnosisRepository,
    diagnosis_repository,
)
from app.services.diagnosis_runner import DiagnosisRunner, build_alert_task
from app.services.service_catalog import service_catalog


class AlertDiagnosisService:
    """Persist requests atomically and let Celery execute them independently."""

    def __init__(
        self,
        repository: DiagnosisRepository | None = None,
        *,
        inline_worker: bool | None = None,
    ) -> None:
        self.repository = repository or InMemoryDiagnosisRepository()
        self.inline_worker = repository is None if inline_worker is None else inline_worker
        self.runner = DiagnosisRunner(self.repository)
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
        return build_alert_task(alert, service_name, service)

    @staticmethod
    def _queue_for_severity(severity: str | None) -> str:
        if (severity or "").lower() == "critical":
            return config.celery_critical_queue
        return config.celery_default_queue

    async def handle_webhook(self, payload: AlertmanagerWebhook) -> list[str]:
        diagnosis_ids: list[str] = []
        for alert in payload.alerts:
            if alert.status == "resolved":
                await self.repository.mark_resolved(alert)
                continue

            event_key = self._event_key(alert)
            diagnosis_id = self._diagnosis_id(event_key)
            service_name, _ = service_catalog.resolve(alert.labels)
            queue_name = self._queue_for_severity(alert.labels.get("severity"))
            job, created = await self.repository.create_or_get_job_and_enqueue(
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
                },
                queue_name,
            )
            diagnosis_ids.append(diagnosis_id)

            should_run = created
            if not created and job["status"] == "interrupted":
                should_run = await self.repository.requeue_interrupted(diagnosis_id, queue_name)
            if should_run and self.inline_worker:
                self._start_inline(diagnosis_id)

        return diagnosis_ids

    async def enqueue_manual(self, session_id: str) -> str:
        diagnosis_id = f"diag-manual-{uuid.uuid4().hex[:16]}"
        await self.repository.create_or_get_job_and_enqueue(
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
            },
            config.celery_default_queue,
        )
        await self.repository.record_event(
            diagnosis_id,
            {
                "type": "status",
                "stage": "job_created",
                "message": "诊断任务已进入独立队列",
                "diagnosis_id": diagnosis_id,
            },
        )
        if self.inline_worker:
            self._start_inline(diagnosis_id)
        return diagnosis_id

    async def diagnose_manual(self, session_id: str) -> AsyncGenerator[dict[str, Any], None]:
        diagnosis_id = await self.enqueue_manual(session_id)
        async for event in self.stream_job(diagnosis_id):
            yield event

    async def stream_job(self, diagnosis_id: str) -> AsyncGenerator[dict[str, Any], None]:
        seen_evidence: set[int] = set()
        while True:
            job = await self.repository.get_job(diagnosis_id)
            if job is None:
                yield {
                    "type": "error",
                    "stage": "queue",
                    "message": "诊断任务不存在",
                    "diagnosis_id": diagnosis_id,
                }
                return
            for evidence in job["evidence"]:
                evidence_id = int(evidence["id"])
                if evidence_id in seen_evidence:
                    continue
                seen_evidence.add(evidence_id)
                yield evidence["payload"]
            if job["status"] in {"completed", "failed", "interrupted"}:
                return
            await asyncio.sleep(0.5)

    def _start_inline(self, diagnosis_id: str) -> None:
        task = asyncio.create_task(
            self.runner.run(
                diagnosis_id,
                celery_task_id=f"inline-{diagnosis_id}",
                worker_name="inline-test-worker",
            ),
            name=f"inline-diagnosis-{diagnosis_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        return await self.repository.get_job(diagnosis_id)

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self.repository.list_jobs()

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


alert_diagnosis_service = AlertDiagnosisService(
    repository=diagnosis_repository,
    inline_worker=False,
)
