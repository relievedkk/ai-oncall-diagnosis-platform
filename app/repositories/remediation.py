"""Persistence and atomic state changes for remediation actions."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import config
from app.db.models import OutboxEvent, RemediationAction
from app.db.session import database_manager

DecisionResult = Literal["approved", "rejected", "expired", "missing", "conflict"]
ExecutionClaim = Literal["execute", "reconcile", "busy", "ignored"]
REMEDIATION_TASK_NAME = "app.tasks.remediation.run_remediation"
REMEDIATION_EVENT_TYPE = "remediation.execute"
ACTIVE_STATUSES = {
    "proposed",
    "awaiting_approval",
    "approved",
    "executing",
    "reconciling",
    "verifying",
    "rolling_back",
}
EXECUTABLE_STATUSES = {"proposed", "approved"}
COMPLETED_STATUSES = {"succeeded", "rolled_back", "failed"}


def utc_now() -> datetime:
    return datetime.now(UTC)


class RemediationRepository(Protocol):
    def proposal_guard(self): ...

    async def create(
        self, values: dict[str, Any], enqueue: bool
    ) -> tuple[dict[str, Any], bool]: ...

    async def get(self, action_id: str) -> dict[str, Any] | None: ...

    async def list(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def compare_and_set(
        self, action_id: str, expected: set[str], **updates: Any
    ) -> bool: ...

    async def decide(
        self, action_id: str, decision: str, principal_id: str, now: datetime
    ) -> DecisionResult: ...

    async def claim_execution(
        self, action_id: str, worker_name: str, lease_seconds: int = 300
    ) -> ExecutionClaim: ...

    async def expire_due_approvals(self, now: datetime) -> int: ...

    async def is_circuit_open(self, target_service: str) -> bool: ...

    async def count_active(self, target_service: str | None = None) -> int: ...

    async def count_for_diagnosis(self, diagnosis_id: str) -> int: ...

    async def last_completed_at(self, target_service: str) -> datetime | None: ...

    async def get_operational_metrics(self) -> dict[str, Any]: ...


class InMemoryRemediationRepository:
    """Behavioral in-memory repository used by service and worker tests."""

    def __init__(self) -> None:
        self._actions: dict[str, dict[str, Any]] = {}
        self._idempotency_index: dict[str, str] = {}
        self.outbox_events: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._proposal_lock = asyncio.Lock()

    @asynccontextmanager
    async def proposal_guard(self) -> AsyncIterator[None]:
        async with self._proposal_lock:
            yield

    async def create(
        self, values: dict[str, Any], enqueue: bool
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            idempotency_key = str(values["idempotency_key"])
            existing_id = self._idempotency_index.get(idempotency_key)
            if existing_id is not None:
                return copy.deepcopy(self._actions[existing_id]), False

            now = utc_now()
            action = copy.deepcopy(values)
            action.setdefault("id", str(uuid4()))
            action.setdefault("created_at", now)
            action.setdefault("updated_at", now)
            self._actions[action["id"]] = action
            self._idempotency_index[idempotency_key] = action["id"]
            if enqueue:
                self.outbox_events.append(_outbox_values(action))
            return copy.deepcopy(action), True

    async def get(self, action_id: str) -> dict[str, Any] | None:
        action = self._actions.get(action_id)
        return copy.deepcopy(action) if action is not None else None

    async def list(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        actions = self._actions.values()
        if status is not None:
            actions = (action for action in actions if action.get("status") == status)
        ordered = sorted(
            actions,
            key=lambda action: action.get("created_at") or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return copy.deepcopy(ordered[:limit])

    async def compare_and_set(
        self, action_id: str, expected: set[str], **updates: Any
    ) -> bool:
        async with self._lock:
            action = self._actions.get(action_id)
            if action is None or action.get("status") not in expected:
                return False
            action.update(copy.deepcopy(updates))
            action["updated_at"] = updates.get("updated_at", utc_now())
            return True

    async def decide(
        self, action_id: str, decision: str, principal_id: str, now: datetime
    ) -> DecisionResult:
        if decision not in {"approve", "reject"}:
            return "conflict"
        async with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return "missing"
            if action.get("status") != "awaiting_approval":
                return "conflict"
            expires_at = action.get("approval_expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                action.update(status="expired", completed_at=now, updated_at=now)
                return "expired"
            if decision == "reject":
                action.update(
                    status="rejected",
                    approved_by=principal_id,
                    completed_at=now,
                    updated_at=now,
                )
                return "rejected"
            action.update(
                status="approved",
                approved_by=principal_id,
                approved_at=now,
                updated_at=now,
            )
            self.outbox_events.append(_outbox_values(action))
            return "approved"

    async def claim_execution(
        self, action_id: str, worker_name: str, lease_seconds: int = 300
    ) -> ExecutionClaim:
        async with self._lock:
            action = self._actions.get(action_id)
            if action is None:
                return "ignored"
            now = utc_now()
            current = str(action.get("status"))
            if current not in EXECUTABLE_STATUSES:
                if current not in {"executing", "reconciling"}:
                    return "ignored"
                updated_at = action.get("updated_at")
                if not isinstance(updated_at, datetime):
                    return "busy"
                if updated_at > now - timedelta(seconds=lease_seconds):
                    return "busy"
                claim: ExecutionClaim = (
                    "reconcile" if action.get("prepared_payload") else "execute"
                )
            else:
                claim = "execute"
            action.update(
                status="reconciling" if claim == "reconcile" else "executing",
                worker_name=worker_name,
                started_at=action.get("started_at") or now,
                updated_at=now,
            )
            return claim

    async def expire_due_approvals(self, now: datetime) -> int:
        expired = 0
        async with self._lock:
            for action in self._actions.values():
                expires_at = action.get("approval_expires_at")
                if (
                    action.get("status") == "awaiting_approval"
                    and isinstance(expires_at, datetime)
                    and expires_at <= now
                ):
                    action.update(status="expired", completed_at=now, updated_at=now)
                    expired += 1
        return expired

    async def is_circuit_open(self, target_service: str) -> bool:
        return any(
            action.get("target_service") == target_service
            and action.get("circuit_breaker_open") is True
            for action in self._actions.values()
        )

    async def count_active(self, target_service: str | None = None) -> int:
        return sum(
            1
            for action in self._actions.values()
            if action.get("status") in ACTIVE_STATUSES
            and (target_service is None or action.get("target_service") == target_service)
        )

    async def count_for_diagnosis(self, diagnosis_id: str) -> int:
        return sum(
            action.get("diagnosis_id") == diagnosis_id
            for action in self._actions.values()
        )

    async def last_completed_at(self, target_service: str) -> datetime | None:
        timestamps = [
            action["completed_at"]
            for action in self._actions.values()
            if action.get("target_service") == target_service
            and action.get("status") in COMPLETED_STATUSES
            and isinstance(action.get("completed_at"), datetime)
        ]
        return max(timestamps, default=None)

    async def get_operational_metrics(self) -> dict[str, Any]:
        async with self._lock:
            action_counts: dict[str, int] = {}
            awaiting_times: list[datetime] = []
            open_circuit_services: set[str] = set()
            for action in self._actions.values():
                status = str(action.get("status", "unknown"))
                action_counts[status] = action_counts.get(status, 0) + 1
                created_at = action.get("created_at")
                if status == "awaiting_approval" and isinstance(created_at, datetime):
                    awaiting_times.append(created_at)
                if action.get("circuit_breaker_open") is True:
                    open_circuit_services.add(str(action.get("target_service", "unknown")))
        oldest_age = 0.0
        if awaiting_times:
            oldest_age = max(0.0, (utc_now() - min(awaiting_times)).total_seconds())
        return {
            "action_counts": action_counts,
            "oldest_awaiting_approval_seconds": oldest_age,
            "open_circuit_services": sorted(open_circuit_services),
        }


class SQLAlchemyRemediationRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessions or database_manager.session_factory

    @asynccontextmanager
    async def proposal_guard(self) -> AsyncIterator[None]:
        async with self._sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": 7_241_947_013},
            )
            yield

    async def create(
        self, values: dict[str, Any], enqueue: bool
    ) -> tuple[dict[str, Any], bool]:
        idempotency_key = str(values["idempotency_key"])
        try:
            async with self._sessions.begin() as session:
                existing = await session.scalar(
                    select(RemediationAction).where(
                        RemediationAction.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return _action_to_dict(existing), False
                action = RemediationAction(**_model_values(values))
                session.add(action)
                await session.flush()
                if enqueue:
                    session.add(OutboxEvent(**_outbox_values(_action_to_dict(action))))
                return _action_to_dict(action), True
        except IntegrityError:
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(RemediationAction).where(
                        RemediationAction.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                return _action_to_dict(existing), False

    async def get(self, action_id: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            action = await session.get(RemediationAction, action_id)
            return _action_to_dict(action) if action is not None else None

    async def list(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        _validate_limit(limit)
        statement = select(RemediationAction)
        if status is not None:
            statement = statement.where(RemediationAction.status == status)
        statement = statement.order_by(RemediationAction.created_at.desc()).limit(limit)
        async with self._sessions() as session:
            actions = list((await session.scalars(statement)).all())
        return [_action_to_dict(action) for action in actions]

    async def compare_and_set(
        self, action_id: str, expected: set[str], **updates: Any
    ) -> bool:
        safe_updates = _update_values(updates)
        safe_updates["updated_at"] = updates.get("updated_at", utc_now())
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(RemediationAction)
                .where(
                    RemediationAction.id == action_id,
                    RemediationAction.status.in_(expected),
                )
                .values(**safe_updates)
            )
            return bool(result.rowcount)

    async def decide(
        self, action_id: str, decision: str, principal_id: str, now: datetime
    ) -> DecisionResult:
        if decision not in {"approve", "reject"}:
            return "conflict"
        async with self._sessions.begin() as session:
            action = await session.scalar(
                select(RemediationAction)
                .where(RemediationAction.id == action_id)
                .with_for_update()
            )
            if action is None:
                return "missing"
            if action.status != "awaiting_approval":
                return "conflict"
            if action.approval_expires_at is None or action.approval_expires_at <= now:
                action.status = "expired"
                action.completed_at = now
                action.updated_at = now
                return "expired"
            action.approved_by = principal_id
            action.updated_at = now
            if decision == "reject":
                action.status = "rejected"
                action.completed_at = now
                return "rejected"
            action.status = "approved"
            action.approved_at = now
            session.add(OutboxEvent(**_outbox_values(_action_to_dict(action))))
            return "approved"

    async def claim_execution(
        self, action_id: str, worker_name: str, lease_seconds: int = 300
    ) -> ExecutionClaim:
        now = utc_now()
        async with self._sessions.begin() as session:
            action = await session.scalar(
                select(RemediationAction)
                .where(RemediationAction.id == action_id)
                .with_for_update()
            )
            if action is None:
                return "ignored"
            if action.status in EXECUTABLE_STATUSES:
                claim: ExecutionClaim = "execute"
            elif action.status in {"executing", "reconciling"}:
                if action.updated_at > now - timedelta(seconds=lease_seconds):
                    return "busy"
                claim = "reconcile" if action.prepared_payload else "execute"
            else:
                return "ignored"
            action.status = "reconciling" if claim == "reconcile" else "executing"
            action.worker_name = worker_name
            action.started_at = action.started_at or now
            action.updated_at = now
            return claim

    async def expire_due_approvals(self, now: datetime) -> int:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(RemediationAction)
                .where(
                    RemediationAction.status == "awaiting_approval",
                    RemediationAction.approval_expires_at <= now,
                )
                .values(status="expired", completed_at=now, updated_at=now)
            )
            return int(result.rowcount or 0)

    async def is_circuit_open(self, target_service: str) -> bool:
        statement = select(func.count()).select_from(RemediationAction).where(
            RemediationAction.target_service == target_service,
            RemediationAction.circuit_breaker_open.is_(True),
        )
        async with self._sessions() as session:
            return bool(await session.scalar(statement) or 0)

    async def count_active(self, target_service: str | None = None) -> int:
        statement = select(func.count()).select_from(RemediationAction).where(
            RemediationAction.status.in_(ACTIVE_STATUSES)
        )
        if target_service is not None:
            statement = statement.where(RemediationAction.target_service == target_service)
        async with self._sessions() as session:
            return int(await session.scalar(statement) or 0)

    async def count_for_diagnosis(self, diagnosis_id: str) -> int:
        statement = select(func.count()).select_from(RemediationAction).where(
            RemediationAction.diagnosis_id == diagnosis_id
        )
        async with self._sessions() as session:
            return int(await session.scalar(statement) or 0)

    async def last_completed_at(self, target_service: str) -> datetime | None:
        statement = select(func.max(RemediationAction.completed_at)).where(
            RemediationAction.target_service == target_service,
            RemediationAction.status.in_(COMPLETED_STATUSES),
        )
        async with self._sessions() as session:
            return await session.scalar(statement)

    async def get_operational_metrics(self) -> dict[str, Any]:
        async with self._sessions() as session:
            count_rows = (
                await session.execute(
                    select(RemediationAction.status, func.count()).group_by(
                        RemediationAction.status
                    )
                )
            ).all()
            oldest_awaiting = await session.scalar(
                select(func.min(RemediationAction.created_at)).where(
                    RemediationAction.status == "awaiting_approval"
                )
            )
            open_circuits = list(
                (
                    await session.scalars(
                        select(RemediationAction.target_service)
                        .where(RemediationAction.circuit_breaker_open.is_(True))
                        .distinct()
                    )
                ).all()
            )
        oldest_age = 0.0
        if oldest_awaiting:
            oldest_age = max(0.0, (utc_now() - oldest_awaiting).total_seconds())
        return {
            "action_counts": {str(status): int(count) for status, count in count_rows},
            "oldest_awaiting_approval_seconds": oldest_age,
            "open_circuit_services": sorted(str(name) for name in open_circuits),
        }


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _model_values(values: dict[str, Any]) -> dict[str, Any]:
    columns = {column.name for column in RemediationAction.__table__.columns}
    result = {key: copy.deepcopy(value) for key, value in values.items() if key in columns}
    result.setdefault("id", str(uuid4()))
    return result


def _update_values(values: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "prepared_payload",
        "result_payload",
        "verification_payload",
        "approved_by",
        "worker_name",
        "approved_at",
        "started_at",
        "completed_at",
        "error",
        "circuit_breaker_open",
        "circuit_opened_at",
        "updated_at",
    }
    return {key: copy.deepcopy(value) for key, value in values.items() if key in allowed}


def _outbox_values(action: dict[str, Any]) -> dict[str, Any]:
    action_id = str(action["id"])
    return {
        "diagnosis_id": str(action["diagnosis_id"]),
        "event_type": REMEDIATION_EVENT_TYPE,
        "task_name": REMEDIATION_TASK_NAME,
        "queue_name": config.celery_remediation_queue,
        "payload": {"args": [action_id], "task_id": action_id},
        "status": "pending",
    }


def _action_to_dict(action: RemediationAction) -> dict[str, Any]:
    return {
        column.name: copy.deepcopy(getattr(action, column.name))
        for column in RemediationAction.__table__.columns
    }


remediation_repository = SQLAlchemyRemediationRepository()
