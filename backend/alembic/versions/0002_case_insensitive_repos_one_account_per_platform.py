"""Case-insensitive repo links, one account per platform per member, code host logins required

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08 23:40:34

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_identities_codehost_login"),
        "identities",
        "kind <> 'codehost' OR username IS NOT NULL",
    )
    op.create_unique_constraint(
        op.f("uq_identities_member_id_platform"), "identities", ["member_id", "platform"]
    )
    op.drop_constraint(
        op.f("uq_repo_connections_workspace_id_provider_owner_name"),
        "repo_connections",
        type_="unique",
    )
    op.create_index(
        "uq_repo_connections_workspace_id_repo",
        "repo_connections",
        [
            "workspace_id",
            "provider",
            sa.literal_column("lower(owner)"),
            sa.literal_column("lower(name)"),
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_repo_connections_workspace_id_repo", table_name="repo_connections")
    op.create_unique_constraint(
        op.f("uq_repo_connections_workspace_id_provider_owner_name"),
        "repo_connections",
        ["workspace_id", "provider", "owner", "name"],
    )
    op.drop_constraint(op.f("uq_identities_member_id_platform"), "identities", type_="unique")
    op.drop_constraint(op.f("ck_identities_codehost_login"), "identities", type_="check")
