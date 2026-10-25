"""Add shared provider-account quota and expiring call leases (backward compatible)."""

import sqlalchemy as sa
from alembic import op

revision = "20261024_0011"
down_revision = "20261023_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_quota_scopes",
        sa.Column("scope", sa.String(128), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("input_bytes", sa.BigInteger(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)))
    op.create_table("model_quota_leases",
        sa.Column("call_id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(128), sa.ForeignKey("model_quota_scopes.scope", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_model_quota_leases_scope", "model_quota_leases", ["scope"])
    op.create_index("ix_model_quota_leases_expires_at", "model_quota_leases", ["expires_at"])


def downgrade():
    op.drop_table("model_quota_leases")
    op.drop_table("model_quota_scopes")
