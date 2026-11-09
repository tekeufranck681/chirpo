"""Message buffer: recent chat messages kept, encrypted, as context for the LLM (D46)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 08:06:43

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_buffer",
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("reply_to_message_id", sa.Text(), nullable=True),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("sender_name", sa.Text(), nullable=False),
        sa.Column("sender_username", sa.Text(), nullable=True),
        sa.Column("text_encrypted", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_message_buffer_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_buffer")),
        sa.UniqueConstraint(
            "workspace_id",
            "platform",
            "chat_id",
            "message_id",
            name=op.f("uq_message_buffer_workspace_id_platform_chat_id_message_id"),
        ),
    )
    op.create_index(
        op.f("ix_message_buffer_received_at"), "message_buffer", ["received_at"], unique=False
    )
    op.create_index(op.f("ix_message_buffer_sent_at"), "message_buffer", ["sent_at"], unique=False)
    op.create_index(
        "ix_message_buffer_workspace_id_chat_sent_at",
        "message_buffer",
        ["workspace_id", "platform", "chat_id", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_buffer_workspace_id_chat_sent_at", table_name="message_buffer")
    op.drop_index(op.f("ix_message_buffer_sent_at"), table_name="message_buffer")
    op.drop_index(op.f("ix_message_buffer_received_at"), table_name="message_buffer")
    op.drop_table("message_buffer")
