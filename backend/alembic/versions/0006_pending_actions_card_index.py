"""Pending actions: find a proposal by the message its card is on (M5.3)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11 17:10:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_pending_actions_workspace_id_chat_id_message_id",
        "pending_actions",
        ["workspace_id", "chat_id", "message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_actions_workspace_id_chat_id_message_id", table_name="pending_actions"
    )
