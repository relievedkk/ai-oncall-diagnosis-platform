"""Add Celery execution metadata and transactional outbox.

Revision ID: 20261008_0002
Revises: 20261007_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0002"
down_revision: str | None = "20261007_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("diagnosis_jobs", sa.Column("celery_task_id", sa.String(255), nullable=True))
    op.add_column("diagnosis_jobs", sa.Column("worker_name", sa.String(255), nullable=True))
    op.add_column(
        "diagnosis_jobs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("diagnosis_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("diagnosis_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_diagnosis_jobs_celery_task_id", "diagnosis_jobs", ["celery_task_id"])
    op.create_index("ix_diagnosis_jobs_lease_expires_at", "diagnosis_jobs", ["lease_expires_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("diagnosis_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("queue_name", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_by", sa.String(255), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["diagnosis_id"], ["diagnosis_jobs.diagnosis_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_events_diagnosis_id", "outbox_events", ["diagnosis_id"])
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_available_at", "outbox_events", ["available_at"])
    op.create_index(
        "ix_outbox_events_dispatch", "outbox_events", ["status", "available_at"]
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_index("ix_diagnosis_jobs_lease_expires_at", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_celery_task_id", table_name="diagnosis_jobs")
    op.drop_column("diagnosis_jobs", "lease_expires_at")
    op.drop_column("diagnosis_jobs", "heartbeat_at")
    op.drop_column("diagnosis_jobs", "attempt_count")
    op.drop_column("diagnosis_jobs", "worker_name")
    op.drop_column("diagnosis_jobs", "celery_task_id")
