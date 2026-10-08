"""Celery task wrapper around the asynchronous diagnosis runner."""

from __future__ import annotations

import asyncio

from celery import Task
from loguru import logger

from app.celery_app import celery_app
from app.config import config
from app.repositories.diagnosis import diagnosis_repository
from app.services.diagnosis_runner import (
    DiagnosisBusyError,
    DiagnosisNotFoundError,
    DiagnosisRunner,
)

_worker_loop: asyncio.AbstractEventLoop | None = None
_runner = DiagnosisRunner(diagnosis_repository)


def _run_async(coroutine):
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    return _worker_loop.run_until_complete(coroutine)


@celery_app.task(
    bind=True,
    name="app.tasks.diagnosis.run_diagnosis",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=config.celery_task_max_retries,
)
def run_diagnosis(self: Task, diagnosis_id: str) -> str:
    """Execute one idempotent diagnosis; PostgreSQL remains the source of truth."""
    task_id = str(self.request.id or diagnosis_id)
    worker_name = str(self.request.hostname or "celery-worker")
    try:
        return _run_async(_runner.run(diagnosis_id, task_id, worker_name))
    except DiagnosisNotFoundError:
        logger.error("Discarding task for missing diagnosis: {}", diagnosis_id)
        return "missing"
    except DiagnosisBusyError as exc:
        countdown = max(5, min(config.diagnosis_lease_seconds, 60))
        raise self.retry(exc=exc, countdown=countdown) from exc
    except Exception as exc:
        retries = int(self.request.retries or 0)
        if retries >= config.celery_task_max_retries:
            raise
        _run_async(diagnosis_repository.prepare_retry(diagnosis_id, str(exc)))
        countdown = min(300, 10 * (2**retries))
        raise self.retry(exc=exc, countdown=countdown) from exc
