"""Observe PostgreSQL queue state and RabbitMQ consumers for Prometheus."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import httpx
from loguru import logger

from app.config import config
from app.core.metrics import (
    DIAGNOSIS_JOBS,
    OUTBOX_EVENTS,
    OUTBOX_OLDEST_PENDING_SECONDS,
    QUEUE_OBSERVER_UP,
    RABBITMQ_QUEUE_CONSUMERS,
    RABBITMQ_QUEUE_MESSAGES,
)
from app.repositories.diagnosis import DiagnosisRepository, diagnosis_repository


class QueueObserver:
    """Periodically export durable queue and authenticated broker state."""

    def __init__(self, repository: DiagnosisRepository = diagnosis_repository) -> None:
        self.repository = repository
        self._task: asyncio.Task[None] | None = None
        self.last_snapshot: dict[str, Any] = {}

    async def rabbitmq_queues(self) -> dict[str, dict[str, int]]:
        url = f"{config.rabbitmq_management_url.rstrip('/')}/api/queues/%2F"
        async with httpx.AsyncClient(
            auth=(config.rabbitmq_default_user, config.rabbitmq_default_pass),
            timeout=3.0,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        wanted = {
            config.celery_critical_queue,
            config.celery_default_queue,
            config.celery_dead_letter_queue,
        }
        result: dict[str, dict[str, int]] = {}
        for item in response.json():
            name = str(item.get("name", ""))
            if name not in wanted:
                continue
            result[name] = {
                "messages_ready": int(item.get("messages_ready", 0)),
                "messages_unacknowledged": int(item.get("messages_unacknowledged", 0)),
                "consumers": int(item.get("consumers", 0)),
            }
        for name in wanted:
            result.setdefault(
                name,
                {"messages_ready": 0, "messages_unacknowledged": 0, "consumers": 0},
            )
        return result

    async def collect_once(self) -> dict[str, Any]:
        database = await self.repository.get_operational_metrics()
        for status in (
            "queued",
            "running",
            "retrying",
            "completed",
            "failed",
            "interrupted",
            "dead_lettered",
        ):
            DIAGNOSIS_JOBS.labels(status=status).set(0)
        for status, count in database["job_counts"].items():
            DIAGNOSIS_JOBS.labels(status=status).set(count)
        for status in ("pending", "publishing", "published"):
            OUTBOX_EVENTS.labels(status=status).set(0)
        for status, count in database["outbox_counts"].items():
            OUTBOX_EVENTS.labels(status=status).set(count)
        OUTBOX_OLDEST_PENDING_SECONDS.set(database["outbox_oldest_pending_seconds"])

        try:
            queues = await self.rabbitmq_queues()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            QUEUE_OBSERVER_UP.set(0)
            self.last_snapshot = {"database": database, "rabbitmq": {}, "error": str(exc)}
            logger.warning("RabbitMQ management scrape failed: {}", exc)
            return self.last_snapshot

        for name, values in queues.items():
            RABBITMQ_QUEUE_MESSAGES.labels(queue=name, state="ready").set(values["messages_ready"])
            RABBITMQ_QUEUE_MESSAGES.labels(queue=name, state="unacknowledged").set(
                values["messages_unacknowledged"]
            )
            RABBITMQ_QUEUE_CONSUMERS.labels(queue=name).set(values["consumers"])
        QUEUE_OBSERVER_UP.set(1)
        self.last_snapshot = {"database": database, "rabbitmq": queues}
        return self.last_snapshot

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.collect_once()
        self._task = asyncio.create_task(self._run(), name="queue-observer")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(config.queue_observer_interval_seconds)
            try:
                await self.collect_once()
            except Exception:
                QUEUE_OBSERVER_UP.set(0)
                logger.exception("Queue observer iteration failed")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


queue_observer = QueueObserver()
