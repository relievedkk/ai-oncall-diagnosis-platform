"""Coordinate searchable knowledge versions across API and workers."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0009"
down_revision = "20261019_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_document_versions",
        sa.Column("collection_name", sa.String(255), primary_key=True),
        sa.Column("source", sa.String(2048), primary_key=True),
        sa.Column("version", sa.String(36), nullable=False),
    )


def downgrade():
    op.drop_table("knowledge_document_versions")
