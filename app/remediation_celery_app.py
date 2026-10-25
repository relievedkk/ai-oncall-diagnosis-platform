"""Credential-minimal Celery application for the isolated remediation worker."""

from celery import Celery
from kombu import Exchange, Queue

from app.config import config
from app.utils import logger as _logging_setup  # noqa: F401
from app.harness.gateway import validate_remediation_adapter

validate_remediation_adapter()

remediation_exchange = Exchange("diagnosis", type="topic", durable=True)
celery_app = Celery(
    "ai_oncall_remediation",
    broker=config.celery_broker_url,
    include=["app.tasks.remediation"],
)
celery_app.conf.update(
    task_queues=(
        Queue(
            config.celery_remediation_queue,
            exchange=remediation_exchange,
            routing_key="remediation",
            durable=True,
            queue_arguments={"x-queue-type": "quorum"},
        ),
    ),
    task_default_queue=config.celery_remediation_queue,
    task_default_exchange="diagnosis",
    task_default_exchange_type="topic",
    task_default_routing_key="remediation",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_ignore_result=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    task_soft_time_limit=config.celery_task_soft_time_limit,
    task_time_limit=config.celery_task_time_limit,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"confirm_publish": True},
    worker_detect_quorum_queues=True,
)
