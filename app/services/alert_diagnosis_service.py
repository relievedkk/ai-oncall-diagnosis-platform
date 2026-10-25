"""Queue-facing diagnosis service for Alertmanager and manual requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from app.config import config
from app.core.data_safety import redact_sensitive
from app.models.alerts import AlertmanagerAlert, AlertmanagerWebhook, canonical_alert_timestamp
from app.repositories.diagnosis import (
    DiagnosisRepository,
    InMemoryDiagnosisRepository,
    diagnosis_repository,
)
from app.services.diagnosis_runner import DiagnosisRunner, build_alert_task
from app.services.service_catalog import service_catalog


class IdempotencyConflictError(ValueError):
    """A principal reused a creation key with a different request."""


def parse_stream_cursor(value: str | int) -> int | str:
    text = str(value)
    if re.fullmatch(r"[0-9]{1,19}", text) and int(text) <= 9223372036854775807:
        return int(text)
    if re.fullmatch(r"terminal:[0-9]{1,10}:(completed|failed|interrupted|dead_lettered)", text):
        return text
    raise ValueError("Invalid diagnosis event cursor")


def _parse_tool_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _format_stream_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Expose persisted tool evidence as a readable SSE event, not raw JSON."""
    payload = redact_sensitive(evidence.get("payload"))
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        public_event = dict(payload)
        if public_event.get("type") == "step_complete":
            # The durable record keeps full results and tool evidence. The browser
            # receives only the documented preview plus a separate readable
            # evidence event, so raw nested tool JSON is never rendered or exposed.
            public_event.pop("result", None)
            public_event.pop("tool_evidence", None)
        return public_event

    evidence_type = str(evidence.get("evidence_type", "tool"))
    source = str(evidence.get("source", "unknown_tool"))
    summary = f"已采集 {evidence_type} 证据，来源：{source}"
    if isinstance(payload, dict) and source == "query_prometheus_alerts":
        result = _parse_tool_result(payload.get("result"))
        if result and result.get("success") is True:
            total = result.get("total")
            if isinstance(total, int):
                summary = f"Prometheus 当前活跃告警：{total} 条"

    return {
        "type": "evidence",
        "stage": "evidence_collected",
        "message": summary,
        "summary": summary,
        "evidence_type": evidence_type,
        "source": source,
    }


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
        starts_at = canonical_alert_timestamp(alert.starts_at)
        if alert.fingerprint:
            return f"{alert.fingerprint}:{starts_at}"
        canonical = json.dumps(
            {"labels": alert.labels, "startsAt": starts_at},
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
        # Revalidate the complete batch even for internal/model_construct callers.
        # No resolve, queue write, or worker launch may precede this boundary.
        payload = AlertmanagerWebhook.model_validate(payload.model_dump(by_alias=True))
        diagnosis_ids: list[str] = []
        for alert in payload.alerts:
            if alert.labels.get("oncall_route") == "operations":
                continue  # Never amplify an operational backlog/failure with more model work.
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
            diagnosis_id = job["diagnosis_id"]  # Preserve IDs from pre-canonical history.
            diagnosis_ids.append(diagnosis_id)

            should_run = created
            if not created and job["status"] == "interrupted":
                should_run = await self.repository.requeue_interrupted(diagnosis_id, queue_name)
            if should_run and self.inline_worker:
                self._start_inline(diagnosis_id)

        return diagnosis_ids

    async def enqueue_manual(
        self, session_id: str, *, owner: str | None = None, idempotency_key: str | None = None,
    ) -> str:
        payload_hash = hashlib.sha256(json.dumps(
            {"session_id": session_id}, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        event_key = f"manual:{uuid.uuid4().hex}"
        metadata = {"session_id": session_id, "source": "manual"}
        if owner is not None:
            metadata.update(owner=owner, payload_hash=payload_hash)
        if idempotency_key is not None:
            if owner is None:
                raise ValueError("Idempotent tasks require an authenticated owner")
            event_key = "manual:v1:" + hashlib.sha256(json.dumps(
                [owner, idempotency_key], separators=(",", ":"),
            ).encode()).hexdigest()
        diagnosis_id = f"diag-manual-{hashlib.sha256(event_key.encode()).hexdigest()[:32]}"
        job, created = await self.repository.create_or_get_job_and_enqueue(
            {
                "diagnosis_id": diagnosis_id,
                "event_key": event_key,
                "fingerprint": None,
                "trigger": "manual",
                "status": "queued",
                "alert_status": "not_applicable",
                "service": "manual",
                "alert_name": "ManualDiagnosis",
                "severity": None,
                "alert": metadata,
            },
            config.celery_default_queue,
        )
        if job.get("alert", {}).get("payload_hash", payload_hash) != payload_hash:
            raise IdempotencyConflictError("Idempotency-Key was already used with another payload")
        diagnosis_id = job["diagnosis_id"]
        if not created:
            return diagnosis_id
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

    async def stream_job(
        self, diagnosis_id: str, after_id: int | str = 0,
    ) -> AsyncGenerator[dict[str, Any], None]:
        cursor = parse_stream_cursor(after_id)
        terminal_cursor = cursor if isinstance(cursor, str) else None
        after_id = cursor if isinstance(cursor, int) else 0
        completion = None
        latest_error = None
        while True:
            job = await self.repository.get_job_stream(diagnosis_id, after_id=after_id, limit=100)
            if job is None:
                yield {
                    "type": "error",
                    "stage": "queue",
                    "message": "诊断任务不存在",
                    "diagnosis_id": diagnosis_id,
                }
                return
            terminal_id = f"terminal:{job.get('attempt_count', 0)}:{job['status']}"
            if terminal_cursor == terminal_id:
                return
            for evidence in job["evidence"]:
                evidence_id = int(evidence["id"])
                after_id = evidence_id
                event = _format_stream_evidence(evidence)
                if event.get("type") == "complete":
                    completion = event
                    continue  # A durable event precedes the job's authority commit.
                if event.get("type") == "error":
                    latest_error = event
                    # An attempt error is progress while Celery owns retry policy.
                    event = {**event, "type": "status", "stage": "attempt_failed"}
                yield {**event, "_event_id": str(evidence_id)}
            if job.get("has_more"):
                continue  # Drain persisted pages before emitting terminal state.
            if job["status"] == "completed":
                if completion is None or completion.get("attempt") != job.get("attempt_count"):
                    completion = {"type": "complete", "response": job.get("report", "")}
                yield redact_sensitive({**completion, "diagnosis_id": diagnosis_id,
                                        "_event_id": terminal_id})
                return
            if job["status"] in {"failed", "interrupted", "dead_lettered"}:
                yield redact_sensitive({
                    "type": "error", "stage": job["status"],
                    "message": job.get("error") or (latest_error or {}).get("message") or "诊断任务未完成",
                    "diagnosis_id": diagnosis_id,
                    "_event_id": terminal_id,
                })
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
        return redact_sensitive(await self.repository.get_job(diagnosis_id))

    async def list_jobs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return redact_sensitive(await self.repository.list_jobs(limit=limit, offset=offset))

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
