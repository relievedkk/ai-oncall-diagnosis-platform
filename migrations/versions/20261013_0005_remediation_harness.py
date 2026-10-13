"""Add deterministic remediation action persistence.

Revision ID: 20261013_0005
Revises: 20261009_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0005"
down_revision: str | None = "20261009_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("diagnosis_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("target_service", sa.String(128), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("intent_payload", sa.JSON(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("policy_decision", sa.String(32), nullable=False),
        sa.Column("policy_reason", sa.String(128), nullable=False),
        sa.Column("prepared_payload", sa.JSON(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("verification_payload", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("worker_name", sa.String(255), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(2048), nullable=True),
        sa.ForeignKeyConstraint(
            ["diagnosis_id"],
            ["diagnosis_jobs.diagnosis_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_remediation_actions_diagnosis_id",
        "remediation_actions",
        ["diagnosis_id"],
    )
    op.create_index(
        "ix_remediation_actions_target_service",
        "remediation_actions",
        ["target_service"],
    )
    op.create_index(
        "ix_remediation_actions_status", "remediation_actions", ["status"]
    )
    op.create_index(
        "ix_remediation_actions_created_at",
        "remediation_actions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("remediation_actions")
