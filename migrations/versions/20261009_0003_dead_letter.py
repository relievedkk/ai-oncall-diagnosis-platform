"""Add terminal dead-letter timestamp to diagnosis jobs.

Revision ID: 20261009_0003
Revises: 20261008_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_0003"
down_revision: str | None = "20261008_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "diagnosis_jobs",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_diagnosis_jobs_dead_lettered_at",
        "diagnosis_jobs",
        ["dead_lettered_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_diagnosis_jobs_dead_lettered_at", table_name="diagnosis_jobs")
    op.drop_column("diagnosis_jobs", "dead_lettered_at")
