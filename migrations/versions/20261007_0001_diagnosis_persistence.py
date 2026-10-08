"""Create durable diagnosis tables.

Revision ID: 20261007_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_jobs",
        sa.Column("diagnosis_id", sa.String(length=64), nullable=False),
        sa.Column("event_key", sa.String(length=512), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("alert_status", sa.String(length=32), nullable=False),
        sa.Column("service", sa.String(length=255), nullable=False),
        sa.Column("alert_name", sa.String(length=255), nullable=True),
        sa.Column("severity", sa.String(length=64), nullable=True),
        sa.Column("alert_payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("diagnosis_id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index("ix_diagnosis_jobs_created_at", "diagnosis_jobs", ["created_at"])
    for column in ("fingerprint", "trigger", "status", "alert_status", "service", "alert_name", "severity"):
        op.create_index(f"ix_diagnosis_jobs_{column}", "diagnosis_jobs", [column])

    op.create_table(
        "diagnosis_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("diagnosis_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.diagnosis_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("diagnosis_id", "sequence", name="uq_diagnosis_step_sequence"),
    )
    op.create_index("ix_diagnosis_steps_diagnosis_id", "diagnosis_steps", ["diagnosis_id"])
    op.create_index("ix_diagnosis_steps_status", "diagnosis_steps", ["status"])

    op.create_table(
        "diagnosis_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("diagnosis_id", sa.String(length=64), nullable=False),
        sa.Column("step_id", sa.BigInteger(), nullable=True),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.diagnosis_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["diagnosis_steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("diagnosis_id", "step_id", "evidence_type", "collected_at"):
        op.create_index(f"ix_diagnosis_evidence_{column}", "diagnosis_evidence", [column])

    op.create_table(
        "diagnosis_reports",
        sa.Column("diagnosis_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["diagnosis_id"], ["diagnosis_jobs.diagnosis_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("diagnosis_id"),
    )


def downgrade() -> None:
    op.drop_table("diagnosis_reports")
    op.drop_table("diagnosis_evidence")
    op.drop_table("diagnosis_steps")
    op.drop_table("diagnosis_jobs")
