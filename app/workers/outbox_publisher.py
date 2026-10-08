"""Publish committed PostgreSQL outbox events to RabbitMQ with confirms."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

from loguru import logger

from app.celery_app import celery_app
from app.config import config
from app.db.session import database_manager
from app.repositories.diagnosis import diagnosis_repository


async def publish_once(publisher_id: str) -> int:
    events = await diagnosis_repository.claim_outbox(publisher_id)
    for event in events:
        try:
            payload = event["payload"]
            await asyncio.to_thread(
                celery_app.send_task,
                event["task_name"],
                args=[payload["diagnosis_id"]],
                task_id=payload["diagnosis_id"],
                queue=event["queue_name"],
                routing_key=(
                    "critical"
                    if event["queue_name"] == config.celery_critical_queue
                    else "default"
                ),
            )
            await diagnosis_repository.mark_outbox_published(event["id"])
            logger.info(
                "Published diagnosis {} to {}",
                payload["diagnosis_id"],
                event["queue_name"],
            )
        except Exception as exc:
            logger.exception("Outbox publish failed for event {}", event["id"])
            await diagnosis_repository.mark_outbox_failed(event["id"], str(exc))
    return len(events)


async def run_forever() -> None:
    await database_manager.initialize()
    publisher_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    logger.info("Outbox publisher started: {}", publisher_id)
    try:
        while True:
            published = await publish_once(publisher_id)
            if not published:
                await asyncio.sleep(config.outbox_poll_interval_seconds)
    finally:
        await database_manager.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("Outbox publisher stopped")
