"""Persist diagnosis model budget across retries and worker replacements."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0010"
down_revision = "20261023_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("diagnosis_jobs", sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("diagnosis_jobs", sa.Column("model_input_bytes", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("diagnosis_jobs", "model_input_bytes")
    op.drop_column("diagnosis_jobs", "model_calls")
