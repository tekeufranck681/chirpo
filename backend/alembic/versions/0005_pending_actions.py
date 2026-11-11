"""Pending actions: proposals waiting for confirmation (M5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11 15:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="proposed", nullable=False),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'executing', 'executed', 'failed', 'rejected', "
            "'modified', 'expired')",
            name=op.f("ck_pending_actions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"],
            ["members.id"],
            name=op.f("fk_pending_actions_confirmed_by_members"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "requested_by"],
            ["members.workspace_id", "members.id"],
            name=op.f("fk_pending_actions_workspace_id_members"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_pending_actions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_actions")),
    )
    op.create_index(
        "ix_pending_actions_expires_at",
        "pending_actions",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'proposed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_actions_expires_at",
        table_name="pending_actions",
        postgresql_where=sa.text("status = 'proposed'"),
    )
    op.drop_table("pending_actions")
