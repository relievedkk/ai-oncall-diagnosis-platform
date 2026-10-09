"""Diagnosis persistence, execution leases, and transactional outbox."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import config
from app.db.models import (
    DiagnosisEvidence,
    DiagnosisJob,
    DiagnosisReport,
    DiagnosisStep,
    OutboxEvent,
)
from app.db.session import database_manager
from app.models.alerts import AlertmanagerAlert

ClaimResult = Literal["claimed", "completed", "busy", "missing"]
DIAGNOSIS_TASK_NAME = "app.tasks.diagnosis.run_diagnosis"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _event_report(event: dict[str, Any]) -> str:
    if event.get("type") == "report":
        return str(event.get("report", ""))
    if event.get("type") == "complete":
        if event.get("response"):
            return str(event["response"])
        diagnosis = event.get("diagnosis")
        if isinstance(diagnosis, dict):
            return str(diagnosis.get("report", ""))
    return ""


class DiagnosisRepository(Protocol):
    async def create_or_get_job(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]: ...

    async def create_or_get_job_and_enqueue(
        self, values: dict[str, Any], queue_name: str
    ) -> tuple[dict[str, Any], bool]: ...

    async def requeue_interrupted(self, diagnosis_id: str, queue_name: str) -> bool: ...

    async def claim_job(
        self, diagnosis_id: str, celery_task_id: str, worker_name: str
    ) -> ClaimResult: ...

    async def heartbeat(self, diagnosis_id: str, celery_task_id: str) -> None: ...

    async def prepare_retry(self, diagnosis_id: str, error: str) -> None: ...

    async def mark_dead_lettered(self, diagnosis_id: str, error: str) -> None: ...

    async def update_job(self, diagnosis_id: str, **values: Any) -> None: ...

    async def record_event(self, diagnosis_id: str, event: dict[str, Any]) -> None: ...

    async def mark_resolved(self, alert: AlertmanagerAlert) -> None: ...

    async def mark_incomplete_as_interrupted(self) -> int: ...

    async def reset_interrupted(self, diagnosis_id: str) -> None: ...

    async def claim_outbox(self, publisher_id: str, limit: int = 20) -> list[dict[str, Any]]: ...

    async def mark_outbox_published(self, event_id: int) -> None: ...

    async def mark_outbox_failed(self, event_id: int, error: str) -> None: ...

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None: ...

    async def list_jobs(self) -> list[dict[str, Any]]: ...

    async def get_operational_metrics(self) -> dict[str, Any]: ...


class InMemoryDiagnosisRepository:
    """Test double that models the same queue and lease semantics as PostgreSQL."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._event_index: dict[str, str] = {}
        self._outbox: dict[int, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._evidence_id = 0
        self._outbox_id = 0

    def _new_job(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        return {
            **copy.deepcopy(values),
            "events": [],
            "steps": [],
            "evidence": [],
            "report": "",
            "error": None,
            "celery_task_id": None,
            "worker_name": None,
            "attempt_count": 0,
            "heartbeat_at": None,
            "lease_expires_at": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "dead_lettered_at": None,
            "resolved_at": None,
        }

    def _enqueue(self, diagnosis_id: str, queue_name: str) -> None:
        self._outbox_id += 1
        now = utc_now().isoformat()
        self._outbox[self._outbox_id] = {
            "id": self._outbox_id,
            "diagnosis_id": diagnosis_id,
            "event_type": "diagnosis.requested",
            "task_name": DIAGNOSIS_TASK_NAME,
            "queue_name": queue_name,
            "payload": {"diagnosis_id": diagnosis_id},
            "status": "pending",
            "attempts": 0,
            "available_at": now,
            "locked_by": None,
            "locked_at": None,
            "published_at": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }

    async def create_or_get_job(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            existing_id = self._event_index.get(values["event_key"])
            if existing_id:
                return copy.deepcopy(self._jobs[existing_id]), False
            job = self._new_job(values)
            self._jobs[job["diagnosis_id"]] = job
            self._event_index[job["event_key"]] = job["diagnosis_id"]
            return copy.deepcopy(job), True

    async def create_or_get_job_and_enqueue(
        self, values: dict[str, Any], queue_name: str
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            existing_id = self._event_index.get(values["event_key"])
            if existing_id:
                return copy.deepcopy(self._jobs[existing_id]), False
            job = self._new_job(values)
            self._jobs[job["diagnosis_id"]] = job
            self._event_index[job["event_key"]] = job["diagnosis_id"]
            self._enqueue(job["diagnosis_id"], queue_name)
            return copy.deepcopy(job), True

    async def requeue_interrupted(self, diagnosis_id: str, queue_name: str) -> bool:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job or job["status"] != "interrupted":
                return False
            job.update(
                status="queued",
                error=None,
                completed_at=None,
                lease_expires_at=None,
                updated_at=utc_now().isoformat(),
            )
            self._enqueue(diagnosis_id, queue_name)
            return True

    async def claim_job(
        self, diagnosis_id: str, celery_task_id: str, worker_name: str
    ) -> ClaimResult:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job:
                return "missing"
            if job["status"] == "completed":
                return "completed"
            lease = _parse_datetime(job.get("lease_expires_at"))
            if job["status"] == "running" and lease and lease > utc_now():
                return "busy"
            if job["status"] not in {"queued", "retrying", "interrupted", "running"}:
                return "busy"
            now = utc_now()
            job.update(
                status="running",
                error=None,
                celery_task_id=celery_task_id,
                worker_name=worker_name,
                attempt_count=int(job.get("attempt_count", 0)) + 1,
                started_at=job.get("started_at") or now.isoformat(),
                completed_at=None,
                heartbeat_at=now.isoformat(),
                lease_expires_at=(now + timedelta(seconds=config.diagnosis_lease_seconds)).isoformat(),
                updated_at=now.isoformat(),
            )
            return "claimed"

    async def heartbeat(self, diagnosis_id: str, celery_task_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job or job.get("celery_task_id") != celery_task_id:
                return
            now = utc_now()
            job["heartbeat_at"] = now.isoformat()
            job["lease_expires_at"] = (
                now + timedelta(seconds=config.diagnosis_lease_seconds)
            ).isoformat()
            job["updated_at"] = now.isoformat()

    async def prepare_retry(self, diagnosis_id: str, error: str) -> None:
        await self.update_job(
            diagnosis_id,
            status="retrying",
            error=error,
            completed_at=None,
            lease_expires_at=None,
        )

    async def mark_dead_lettered(self, diagnosis_id: str, error: str) -> None:
        await self.update_job(
            diagnosis_id,
            status="dead_lettered",
            error=error,
            dead_lettered_at=utc_now().isoformat(),
            lease_expires_at=None,
        )

    async def update_job(self, diagnosis_id: str, **values: Any) -> None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job:
                return
            now = utc_now().isoformat()
            status = values.get("status")
            if status == "running" and not job.get("started_at"):
                job["started_at"] = now
            if status in {"completed", "failed", "interrupted", "dead_lettered"}:
                job["completed_at"] = now
                job["lease_expires_at"] = None
            job.update(copy.deepcopy(values))
            job["updated_at"] = now

    async def record_event(self, diagnosis_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job:
                return
            safe_event = _json_safe(event)
            event_type = str(event.get("type", "event"))
            step_id: int | None = None
            if event_type == "plan":
                pending_names = {s["name"] for s in job["steps"] if s["status"] == "planned"}
                for name in event.get("plan", []):
                    if str(name) in pending_names:
                        continue
                    job["steps"].append(
                        {
                            "id": len(job["steps"]) + 1,
                            "sequence": len(job["steps"]) + 1,
                            "name": str(name),
                            "stage": str(event.get("stage", "planner")),
                            "status": "planned",
                            "result": None,
                            "created_at": utc_now().isoformat(),
                            "started_at": None,
                            "completed_at": None,
                        }
                    )
                    pending_names.add(str(name))
            elif event_type == "step_complete":
                name = str(event.get("current_step", "unknown step"))
                step = next(
                    (s for s in job["steps"] if s["status"] == "planned" and s["name"] == name),
                    None,
                )
                if step is None:
                    step = {
                        "id": len(job["steps"]) + 1,
                        "sequence": len(job["steps"]) + 1,
                        "name": name,
                        "stage": str(event.get("stage", "executor")),
                        "status": "planned",
                        "result": None,
                        "created_at": utc_now().isoformat(),
                        "started_at": None,
                        "completed_at": None,
                    }
                    job["steps"].append(step)
                step["status"] = "completed"
                step["result"] = str(event.get("result", ""))
                step["started_at"] = step["started_at"] or utc_now().isoformat()
                step["completed_at"] = utc_now().isoformat()
                step_id = step["id"]

            self._evidence_id += 1
            evidence = {
                "id": self._evidence_id,
                "step_id": step_id,
                "evidence_type": event_type,
                "source": str(event.get("stage", "aiops")),
                "payload": safe_event,
                "collected_at": utc_now().isoformat(),
            }
            job["evidence"].append(evidence)
            for tool_item in event.get("tool_evidence", []):
                self._evidence_id += 1
                item = _json_safe(tool_item)
                job["evidence"].append(
                    {
                        "id": self._evidence_id,
                        "step_id": step_id,
                        "evidence_type": str(item.get("evidence_type", "tool")),
                        "source": str(item.get("source", "unknown_tool")),
                        "payload": item,
                        "collected_at": utc_now().isoformat(),
                    }
                )
            job["events"].append(safe_event)
            report = _event_report(event)
            if report:
                job["report"] = report
            job["updated_at"] = utc_now().isoformat()

    async def mark_resolved(self, alert: AlertmanagerAlert) -> None:
        async with self._lock:
            for job in self._jobs.values():
                if not _matches_alert(job, alert):
                    continue
                job["alert_status"] = "resolved"
                job["resolved_at"] = alert.ends_at or utc_now().isoformat()
                job["updated_at"] = utc_now().isoformat()

    async def mark_incomplete_as_interrupted(self) -> int:
        async with self._lock:
            count = 0
            for job in self._jobs.values():
                if job["status"] in {"queued", "running"}:
                    job["status"] = "interrupted"
                    job["error"] = "application restarted before diagnosis completed"
                    job["completed_at"] = utc_now().isoformat()
                    job["lease_expires_at"] = None
                    job["updated_at"] = utc_now().isoformat()
                    count += 1
            return count

    async def reset_interrupted(self, diagnosis_id: str) -> None:
        await self.update_job(
            diagnosis_id, status="queued", error=None, started_at=None, completed_at=None
        )

    async def claim_outbox(self, publisher_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            now = utc_now()
            claimed: list[dict[str, Any]] = []
            for event in self._outbox.values():
                if len(claimed) >= limit:
                    break
                if event["status"] != "pending":
                    continue
                if _parse_datetime(event["available_at"]) > now:
                    continue
                event.update(
                    status="publishing",
                    locked_by=publisher_id,
                    locked_at=now.isoformat(),
                    attempts=event["attempts"] + 1,
                    updated_at=now.isoformat(),
                )
                claimed.append(copy.deepcopy(event))
            return claimed

    async def mark_outbox_published(self, event_id: int) -> None:
        async with self._lock:
            event = self._outbox[event_id]
            now = utc_now().isoformat()
            event.update(status="published", published_at=now, updated_at=now, last_error=None)

    async def mark_outbox_failed(self, event_id: int, error: str) -> None:
        async with self._lock:
            event = self._outbox[event_id]
            delay = min(300, 2 ** min(event["attempts"], 8))
            now = utc_now()
            event.update(
                status="pending",
                available_at=(now + timedelta(seconds=delay)).isoformat(),
                locked_by=None,
                locked_at=None,
                last_error=error,
                updated_at=now.isoformat(),
            )

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            return copy.deepcopy(job) if job else None

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._lock:
            jobs = copy.deepcopy(list(self._jobs.values()))
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)

    async def get_operational_metrics(self) -> dict[str, Any]:
        async with self._lock:
            job_counts: dict[str, int] = {}
            for job in self._jobs.values():
                status = str(job["status"])
                job_counts[status] = job_counts.get(status, 0) + 1
            outbox_counts: dict[str, int] = {}
            pending_times: list[datetime] = []
            for event in self._outbox.values():
                status = str(event["status"])
                outbox_counts[status] = outbox_counts.get(status, 0) + 1
                if status in {"pending", "publishing"}:
                    created_at = _parse_datetime(event["created_at"])
                    if created_at:
                        pending_times.append(created_at)
        oldest_age = 0.0
        if pending_times:
            oldest_age = max(0.0, (utc_now() - min(pending_times)).total_seconds())
        return {
            "job_counts": job_counts,
            "outbox_counts": outbox_counts,
            "outbox_oldest_pending_seconds": oldest_age,
        }


class SQLAlchemyDiagnosisRepository:
    """PostgreSQL repository used by API, publisher, and Celery workers."""

    def __init__(self) -> None:
        self._sessions = database_manager.session_factory

    @staticmethod
    def _new_job(values: dict[str, Any]) -> DiagnosisJob:
        return DiagnosisJob(
            diagnosis_id=values["diagnosis_id"],
            event_key=values["event_key"],
            fingerprint=values.get("fingerprint"),
            trigger=values.get("trigger", "alertmanager"),
            status=values.get("status", "queued"),
            alert_status=values.get("alert_status", "firing"),
            service=values.get("service", "unknown"),
            alert_name=values.get("alert_name"),
            severity=values.get("severity"),
            alert_payload=_json_safe(values.get("alert", {})),
        )

    @staticmethod
    def _new_outbox(diagnosis_id: str, queue_name: str) -> OutboxEvent:
        return OutboxEvent(
            diagnosis_id=diagnosis_id,
            event_type="diagnosis.requested",
            task_name=DIAGNOSIS_TASK_NAME,
            queue_name=queue_name,
            payload={"diagnosis_id": diagnosis_id},
            status="pending",
            attempts=0,
            available_at=utc_now(),
        )

    async def create_or_get_job(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await self._create(values, queue_name=None)

    async def create_or_get_job_and_enqueue(
        self, values: dict[str, Any], queue_name: str
    ) -> tuple[dict[str, Any], bool]:
        return await self._create(values, queue_name=queue_name)

    async def _create(
        self, values: dict[str, Any], queue_name: str | None
    ) -> tuple[dict[str, Any], bool]:
        job = self._new_job(values)
        async with self._sessions() as session:
            session.add(job)
            if queue_name:
                session.add(self._new_outbox(job.diagnosis_id, queue_name))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(DiagnosisJob).where(DiagnosisJob.event_key == values["event_key"])
                )
                if existing is None:
                    raise
                loaded = await self._load_job(existing.diagnosis_id)
                if loaded is None:  # pragma: no cover
                    raise RuntimeError("existing diagnosis disappeared") from None
                return loaded, False
        created = await self._load_job(job.diagnosis_id)
        if created is None:  # pragma: no cover
            raise RuntimeError(f"diagnosis job was not persisted: {job.diagnosis_id}")
        return created, True

    async def requeue_interrupted(self, diagnosis_id: str, queue_name: str) -> bool:
        async with self._sessions.begin() as session:
            job = await session.get(DiagnosisJob, diagnosis_id, with_for_update=True)
            if not job or job.status != "interrupted":
                return False
            job.status = "queued"
            job.error = None
            job.completed_at = None
            job.lease_expires_at = None
            job.updated_at = utc_now()
            session.add(self._new_outbox(diagnosis_id, queue_name))
            return True

    async def claim_job(
        self, diagnosis_id: str, celery_task_id: str, worker_name: str
    ) -> ClaimResult:
        async with self._sessions.begin() as session:
            job = await session.get(DiagnosisJob, diagnosis_id, with_for_update=True)
            if job is None:
                return "missing"
            if job.status == "completed":
                return "completed"
            now = utc_now()
            if job.status == "running" and job.lease_expires_at and job.lease_expires_at > now:
                return "busy"
            if job.status not in {"queued", "retrying", "interrupted", "running"}:
                return "busy"
            job.status = "running"
            job.error = None
            job.celery_task_id = celery_task_id
            job.worker_name = worker_name
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.completed_at = None
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=config.diagnosis_lease_seconds)
            job.updated_at = now
            return "claimed"

    async def heartbeat(self, diagnosis_id: str, celery_task_id: str) -> None:
        now = utc_now()
        async with self._sessions.begin() as session:
            await session.execute(
                update(DiagnosisJob)
                .where(
                    DiagnosisJob.diagnosis_id == diagnosis_id,
                    DiagnosisJob.celery_task_id == celery_task_id,
                    DiagnosisJob.status == "running",
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=config.diagnosis_lease_seconds),
                    updated_at=now,
                )
            )

    async def prepare_retry(self, diagnosis_id: str, error: str) -> None:
        await self.update_job(
            diagnosis_id,
            status="retrying",
            error=error,
            completed_at=None,
            lease_expires_at=None,
        )

    async def mark_dead_lettered(self, diagnosis_id: str, error: str) -> None:
        await self.update_job(
            diagnosis_id,
            status="dead_lettered",
            error=error,
            dead_lettered_at=utc_now(),
            lease_expires_at=None,
        )

    async def update_job(self, diagnosis_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "alert_status",
            "error",
            "started_at",
            "completed_at",
            "resolved_at",
            "dead_lettered_at",
            "celery_task_id",
            "worker_name",
            "heartbeat_at",
            "lease_expires_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        now = utc_now()
        status = updates.get("status")
        if status == "running" and "started_at" not in updates:
            updates["started_at"] = now
        if status in {"completed", "failed", "interrupted", "dead_lettered"}:
            updates.setdefault("completed_at", now)
            updates.setdefault("lease_expires_at", None)
        for key in (
            "started_at",
            "completed_at",
            "resolved_at",
            "dead_lettered_at",
            "heartbeat_at",
            "lease_expires_at",
        ):
            if key in updates:
                updates[key] = _parse_datetime(updates[key])
        updates["updated_at"] = now
        async with self._sessions.begin() as session:
            await session.execute(
                update(DiagnosisJob)
                .where(DiagnosisJob.diagnosis_id == diagnosis_id)
                .values(**updates)
            )

    async def record_event(self, diagnosis_id: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "event"))
        source = str(event.get("stage", "aiops"))
        now = utc_now()
        async with self._sessions.begin() as session:
            job = await session.get(DiagnosisJob, diagnosis_id)
            if job is None:
                return
            step: DiagnosisStep | None = None
            if event_type == "plan":
                pending_names = set(
                    (
                        await session.scalars(
                            select(DiagnosisStep.name).where(
                                DiagnosisStep.diagnosis_id == diagnosis_id,
                                DiagnosisStep.status == "planned",
                            )
                        )
                    ).all()
                )
                max_sequence = await session.scalar(
                    select(func.max(DiagnosisStep.sequence)).where(
                        DiagnosisStep.diagnosis_id == diagnosis_id
                    )
                )
                sequence = int(max_sequence or 0)
                for name in event.get("plan", []):
                    name = str(name)
                    if name in pending_names:
                        continue
                    sequence += 1
                    session.add(
                        DiagnosisStep(
                            diagnosis_id=diagnosis_id,
                            sequence=sequence,
                            name=name,
                            stage=source,
                            status="planned",
                        )
                    )
                    pending_names.add(name)
            elif event_type == "step_complete":
                name = str(event.get("current_step", "unknown step"))
                step = await session.scalar(
                    select(DiagnosisStep)
                    .where(
                        DiagnosisStep.diagnosis_id == diagnosis_id,
                        DiagnosisStep.name == name,
                        DiagnosisStep.status == "planned",
                    )
                    .order_by(DiagnosisStep.sequence)
                    .limit(1)
                )
                if step is None:
                    max_sequence = await session.scalar(
                        select(func.max(DiagnosisStep.sequence)).where(
                            DiagnosisStep.diagnosis_id == diagnosis_id
                        )
                    )
                    step = DiagnosisStep(
                        diagnosis_id=diagnosis_id,
                        sequence=int(max_sequence or 0) + 1,
                        name=name,
                        stage=source,
                        status="planned",
                    )
                    session.add(step)
                step.status = "completed"
                step.result = str(event.get("result", ""))
                step.started_at = step.started_at or now
                step.completed_at = now
                await session.flush()
            session.add(
                DiagnosisEvidence(
                    diagnosis_id=diagnosis_id,
                    step_id=step.id if step else None,
                    evidence_type=event_type,
                    source=source,
                    payload=_json_safe(event),
                    collected_at=now,
                )
            )
            for tool_item in event.get("tool_evidence", []):
                item = _json_safe(tool_item)
                session.add(
                    DiagnosisEvidence(
                        diagnosis_id=diagnosis_id,
                        step_id=step.id if step else None,
                        evidence_type=str(item.get("evidence_type", "tool")),
                        source=str(item.get("source", "unknown_tool")),
                        payload=item,
                        collected_at=now,
                    )
                )
            report_content = _event_report(event)
            if report_content:
                report = await session.get(DiagnosisReport, diagnosis_id)
                if report is None:
                    session.add(
                        DiagnosisReport(
                            diagnosis_id=diagnosis_id,
                            content=report_content,
                            format="markdown",
                        )
                    )
                else:
                    report.content = report_content
                    report.updated_at = now
            job.updated_at = now

    async def mark_resolved(self, alert: AlertmanagerAlert) -> None:
        async with self._sessions.begin() as session:
            statement = select(DiagnosisJob).where(DiagnosisJob.alert_status == "firing")
            if alert.fingerprint:
                statement = statement.where(DiagnosisJob.fingerprint == alert.fingerprint)
            jobs = list((await session.scalars(statement)).all())
            for job in jobs:
                if not _matches_alert(_job_identity(job), alert):
                    continue
                job.alert_status = "resolved"
                job.resolved_at = _parse_datetime(alert.ends_at) or utc_now()
                job.updated_at = utc_now()

    async def mark_incomplete_as_interrupted(self) -> int:
        now = utc_now()
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(DiagnosisJob)
                .where(DiagnosisJob.status.in_(("queued", "running")))
                .values(
                    status="interrupted",
                    error="application restarted before diagnosis completed",
                    completed_at=now,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    async def reset_interrupted(self, diagnosis_id: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(DiagnosisJob)
                .where(
                    DiagnosisJob.diagnosis_id == diagnosis_id,
                    DiagnosisJob.status == "interrupted",
                )
                .values(
                    status="queued",
                    error=None,
                    started_at=None,
                    completed_at=None,
                    lease_expires_at=None,
                    updated_at=utc_now(),
                )
            )

    async def claim_outbox(self, publisher_id: str, limit: int = 20) -> list[dict[str, Any]]:
        now = utc_now()
        stale_lock = now - timedelta(minutes=5)
        async with self._sessions.begin() as session:
            events = list(
                (
                    await session.scalars(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.available_at <= now,
                            or_(
                                OutboxEvent.status == "pending",
                                (
                                    (OutboxEvent.status == "publishing")
                                    & (OutboxEvent.locked_at < stale_lock)
                                ),
                            ),
                        )
                        .order_by(OutboxEvent.created_at)
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).all()
            )
            result = []
            for event in events:
                event.status = "publishing"
                event.locked_by = publisher_id
                event.locked_at = now
                event.attempts += 1
                event.updated_at = now
                result.append(_outbox_to_dict(event))
            return result

    async def mark_outbox_published(self, event_id: int) -> None:
        now = utc_now()
        async with self._sessions.begin() as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(
                    status="published",
                    published_at=now,
                    locked_by=None,
                    locked_at=None,
                    last_error=None,
                    updated_at=now,
                )
            )

    async def mark_outbox_failed(self, event_id: int, error: str) -> None:
        async with self._sessions.begin() as session:
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if not event:
                return
            now = utc_now()
            delay = min(300, 2 ** min(event.attempts, 8))
            event.status = "pending"
            event.available_at = now + timedelta(seconds=delay)
            event.locked_by = None
            event.locked_at = None
            event.last_error = error
            event.updated_at = now

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        return await self._load_job(diagnosis_id)

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._sessions() as session:
            jobs = list(
                (
                    await session.scalars(
                        select(DiagnosisJob)
                        .options(
                            selectinload(DiagnosisJob.steps),
                            selectinload(DiagnosisJob.evidence),
                            selectinload(DiagnosisJob.report),
                        )
                        .order_by(DiagnosisJob.created_at.desc())
                    )
                ).all()
            )
            return [_job_to_dict(job) for job in jobs]

    async def get_operational_metrics(self) -> dict[str, Any]:
        async with self._sessions() as session:
            job_rows = (
                await session.execute(
                    select(DiagnosisJob.status, func.count()).group_by(DiagnosisJob.status)
                )
            ).all()
            outbox_rows = (
                await session.execute(
                    select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status)
                )
            ).all()
            oldest_pending = await session.scalar(
                select(func.min(OutboxEvent.created_at)).where(
                    OutboxEvent.status.in_(("pending", "publishing"))
                )
            )
        oldest_age = 0.0
        if oldest_pending:
            oldest_age = max(0.0, (utc_now() - oldest_pending).total_seconds())
        return {
            "job_counts": {str(status): int(count) for status, count in job_rows},
            "outbox_counts": {str(status): int(count) for status, count in outbox_rows},
            "outbox_oldest_pending_seconds": oldest_age,
        }

    async def _load_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            job = await session.scalar(
                select(DiagnosisJob)
                .where(DiagnosisJob.diagnosis_id == diagnosis_id)
                .options(
                    selectinload(DiagnosisJob.steps),
                    selectinload(DiagnosisJob.evidence),
                    selectinload(DiagnosisJob.report),
                )
            )
            return _job_to_dict(job) if job else None


def _outbox_to_dict(event: OutboxEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "diagnosis_id": event.diagnosis_id,
        "event_type": event.event_type,
        "task_name": event.task_name,
        "queue_name": event.queue_name,
        "payload": event.payload,
        "status": event.status,
        "attempts": event.attempts,
    }


def _job_identity(job: DiagnosisJob) -> dict[str, Any]:
    return {"fingerprint": job.fingerprint, "alert": job.alert_payload}


def _matches_alert(job: dict[str, Any], alert: AlertmanagerAlert) -> bool:
    if alert.fingerprint:
        return job.get("fingerprint") == alert.fingerprint
    labels = job.get("alert", {}).get("labels", {})
    return all(
        alert.labels.get(key, "") == labels.get(key, "")
        for key in ("alertname", "service", "job", "instance")
    )


def _job_to_dict(job: DiagnosisJob) -> dict[str, Any]:
    steps = [
        {
            "id": step.id,
            "sequence": step.sequence,
            "name": step.name,
            "stage": step.stage,
            "status": step.status,
            "result": step.result,
            "created_at": _iso(step.created_at),
            "started_at": _iso(step.started_at),
            "completed_at": _iso(step.completed_at),
        }
        for step in sorted(job.steps, key=lambda item: item.sequence)
    ]
    evidence = [
        {
            "id": item.id,
            "step_id": item.step_id,
            "evidence_type": item.evidence_type,
            "source": item.source,
            "payload": item.payload,
            "collected_at": _iso(item.collected_at),
        }
        for item in sorted(job.evidence, key=lambda item: item.id)
    ]
    return {
        "diagnosis_id": job.diagnosis_id,
        "event_key": job.event_key,
        "fingerprint": job.fingerprint,
        "trigger": job.trigger,
        "status": job.status,
        "alert_status": job.alert_status,
        "service": job.service,
        "alert_name": job.alert_name,
        "severity": job.severity,
        "alert": job.alert_payload,
        "steps": steps,
        "evidence": evidence,
        "events": [item["payload"] for item in evidence],
        "report": job.report.content if job.report else "",
        "error": job.error,
        "celery_task_id": job.celery_task_id,
        "worker_name": job.worker_name,
        "attempt_count": job.attempt_count,
        "heartbeat_at": _iso(job.heartbeat_at),
        "lease_expires_at": _iso(job.lease_expires_at),
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "dead_lettered_at": _iso(job.dead_lettered_at),
        "resolved_at": _iso(job.resolved_at),
    }


diagnosis_repository = SQLAlchemyDiagnosisRepository()
