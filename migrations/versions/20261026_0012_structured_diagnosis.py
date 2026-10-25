"""Persist structured diagnosis results alongside legacy Markdown."""

from alembic import op
import sqlalchemy as sa

revision = "20261026_0012"
down_revision = "20261024_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("diagnosis_reports", sa.Column("structured_result", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("diagnosis_reports", "structured_result")
