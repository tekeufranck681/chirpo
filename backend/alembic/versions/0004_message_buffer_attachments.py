"""Message buffer: the files sent with a message, as encrypted metadata (D50)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11 09:12:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_buffer", sa.Column("attachments_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_buffer", "attachments_encrypted")
