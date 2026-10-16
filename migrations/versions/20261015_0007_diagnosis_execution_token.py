"""Fence diagnosis writes with a per-claim execution identity.

Revision ID: 20261015_0007
Revises: 20261014_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261015_0007"
down_revision: str | None = "20261014_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("diagnosis_jobs", sa.Column("execution_token", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("diagnosis_jobs", "execution_token")
