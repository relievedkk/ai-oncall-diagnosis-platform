"""Persist remediation circuit-breaker state.

Revision ID: 20261014_0006
Revises: 20261013_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0006"
down_revision: str | None = "20261013_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "remediation_actions",
        sa.Column(
            "circuit_breaker_open",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "remediation_actions",
        sa.Column("circuit_opened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_remediation_actions_circuit_breaker_open",
        "remediation_actions",
        ["circuit_breaker_open"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remediation_actions_circuit_breaker_open",
        table_name="remediation_actions",
    )
    op.drop_column("remediation_actions", "circuit_opened_at")
    op.drop_column("remediation_actions", "circuit_breaker_open")
