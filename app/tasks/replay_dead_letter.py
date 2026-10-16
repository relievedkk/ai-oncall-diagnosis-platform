"""Explicit operator replay: python -m app.tasks.replay_dead_letter DIAGNOSIS_ID."""

import argparse
import asyncio

from app.config import config
from app.db.session import database_manager
from app.repositories.diagnosis import diagnosis_repository


async def replay(diagnosis_id: str) -> bool:
    try:
        return await diagnosis_repository.requeue_dead_lettered(
            diagnosis_id, config.celery_default_queue,
        )
    finally:
        await database_manager.close()


def main():
    parser = argparse.ArgumentParser(description="Requeue a dead-lettered diagnosis via the durable outbox")
    parser.add_argument("diagnosis_id")
    args = parser.parse_args()
    if not asyncio.run(replay(args.diagnosis_id)):
        parser.exit(1, "Diagnosis is missing or is not dead-lettered; nothing was queued.\n")
    print("Diagnosis queued in durable outbox.")


if __name__ == "__main__":
    main()
