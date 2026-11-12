"""Pending actions: a status for changes that were undone (M5.6)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12 01:10:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = (
    "'proposed', 'confirmed', 'executing', 'executed', 'failed', 'rejected', 'modified', 'expired'"
)
AFTER = BEFORE + ", 'undone'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_pending_actions_status"), "pending_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_pending_actions_status"), "pending_actions", f"status IN ({AFTER})"
    )


def downgrade() -> None:
    # Rows already undone would break the old constraint: they become plain executed ones.
    op.execute("UPDATE pending_actions SET status = 'executed' WHERE status = 'undone'")
    op.drop_constraint(op.f("ck_pending_actions_status"), "pending_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_pending_actions_status"), "pending_actions", f"status IN ({BEFORE})"
    )
