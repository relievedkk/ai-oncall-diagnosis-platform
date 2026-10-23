"""Interaction metadata and user-owned feedback, separated from evaluation data."""

from alembic import op
import sqlalchemy as sa

revision = "20261019_0008"
down_revision = "20261015_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_interactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("data_origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("first_content_ms", sa.Integer()),
        sa.Column("answer_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for field in ("user_id", "data_origin", "created_at"):
        op.create_index("ix_chat_interactions_" + field, "chat_interactions", [field])
    op.create_table(
        "answer_feedback",
        sa.Column(
            "interaction_id",
            sa.String(36),
            sa.ForeignKey("chat_interactions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("comment", sa.String(1000), nullable=False, server_default=""),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("answer_feedback")
    op.drop_table("chat_interactions")
