"""Declare all durable Celery queues before publishers and workers start."""

from loguru import logger

from app.celery_app import celery_app


def main() -> None:
    with celery_app.connection_for_write() as connection:
        channel = connection.channel()
        try:
            for queue in celery_app.conf.task_queues:
                queue.bind(channel).declare()
                logger.info("Declared durable RabbitMQ queue: {}", queue.name)
        finally:
            channel.close()


if __name__ == "__main__":
    main()
