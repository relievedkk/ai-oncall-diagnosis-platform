"""Diagnosis persistence implementations.

Production uses PostgreSQL through SQLAlchemy.  The in-memory implementation is
kept as an explicit test double so unit tests do not need external services.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.db.models import DiagnosisEvidence, DiagnosisJob, DiagnosisReport, DiagnosisStep
from app.db.session import database_manager
from app.models.alerts import AlertmanagerAlert


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

    async def update_job(self, diagnosis_id: str, **values: Any) -> None: ...

    async def record_event(self, diagnosis_id: str, event: dict[str, Any]) -> None: ...

    async def mark_resolved(self, alert: AlertmanagerAlert) -> None: ...

    async def mark_incomplete_as_interrupted(self) -> int: ...

    async def reset_interrupted(self, diagnosis_id: str) -> None: ...

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None: ...

    async def list_jobs(self) -> list[dict[str, Any]]: ...


class InMemoryDiagnosisRepository:
    """Repository test double with the same public contract as PostgreSQL."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._event_index: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._evidence_id = 0

    async def create_or_get_job(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            existing_id = self._event_index.get(values["event_key"])
            if existing_id:
                return copy.deepcopy(self._jobs[existing_id]), False
            now = utc_now().isoformat()
            job = {
                **copy.deepcopy(values),
                "events": [],
                "steps": [],
                "evidence": [],
                "report": "",
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "resolved_at": None,
            }
            self._jobs[job["diagnosis_id"]] = job
            self._event_index[job["event_key"]] = job["diagnosis_id"]
            return copy.deepcopy(job), True

    async def update_job(self, diagnosis_id: str, **values: Any) -> None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            if not job:
                return
            now = utc_now().isoformat()
            status = values.get("status")
            if status == "running" and not job.get("started_at"):
                job["started_at"] = now
            if status in {"completed", "failed", "interrupted"}:
                job["completed_at"] = now
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
                    job["updated_at"] = utc_now().isoformat()
                    count += 1
            return count

    async def reset_interrupted(self, diagnosis_id: str) -> None:
        await self.update_job(
            diagnosis_id, status="queued", error=None, started_at=None, completed_at=None
        )

    async def get_job(self, diagnosis_id: str) -> dict[str, Any] | None:
        async with self._lock:
            job = self._jobs.get(diagnosis_id)
            return copy.deepcopy(job) if job else None

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._lock:
            jobs = copy.deepcopy(list(self._jobs.values()))
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)


class SQLAlchemyDiagnosisRepository:
    """Durable PostgreSQL repository used by the running application."""

    def __init__(self) -> None:
        self._sessions = database_manager.session_factory

    async def create_or_get_job(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        job = DiagnosisJob(
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
        async with self._sessions() as session:
            session.add(job)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(DiagnosisJob).where(DiagnosisJob.event_key == values["event_key"])
                )
                if existing is None:
                    raise
                return await self._load_job(existing.diagnosis_id), False
        created = await self._load_job(job.diagnosis_id)
        if created is None:  # pragma: no cover - defensive consistency guard
            raise RuntimeError(f"diagnosis job was not persisted: {job.diagnosis_id}")
        return created, True

    async def update_job(self, diagnosis_id: str, **values: Any) -> None:
        allowed = {"status", "alert_status", "error", "started_at", "completed_at", "resolved_at"}
        updates = {key: value for key, value in values.items() if key in allowed}
        now = utc_now()
        status = updates.get("status")
        if status == "running" and "started_at" not in updates:
            updates["started_at"] = now
        if status in {"completed", "failed", "interrupted"} and "completed_at" not in updates:
            updates["completed_at"] = now
        for key in ("started_at", "completed_at", "resolved_at"):
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
                    updated_at=utc_now(),
                )
            )

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
        # Backward-compatible workflow event list used by existing clients.
        "events": [item["payload"] for item in evidence],
        "report": job.report.content if job.report else "",
        "error": job.error,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "resolved_at": _iso(job.resolved_at),
    }


diagnosis_repository = SQLAlchemyDiagnosisRepository()
