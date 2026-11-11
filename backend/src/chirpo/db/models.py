"""SQLAlchemy models. Converted to domain models at the repository boundary.

Enum-like columns are text + CHECK built from the core enums (D14): easier to migrate than PG ENUMs.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chirpo.core.domain import AuditResult, IdentityKind
from chirpo.core.permissions import Role

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMPTY_JSON = text("'{}'::jsonb")


def one_of(column: str, values: type[StrEnum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({allowed})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())


class TimestampsMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class WorkspaceScopedMixin:
    # No index here: most tables get one from a unique constraint that starts with workspace_id.
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))


class Workspace(IdMixin, TimestampsMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)


class ChatConnection(IdMixin, TimestampsMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "chat_connections"
    __table_args__ = (
        UniqueConstraint("platform", "external_chat_id"),
        Index("ix_chat_connections_workspace_id", "workspace_id"),
    )

    platform: Mapped[str] = mapped_column(Text)
    external_chat_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    notify_prefs: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)


class RepoConnection(IdMixin, TimestampsMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "repo_connections"
    __table_args__ = (
        # Repo names are case-insensitive on code hosts: "Owner/Repo" and "owner/repo" are one repo.
        Index(
            "uq_repo_connections_workspace_id_repo",
            "workspace_id",
            "provider",
            text("lower(owner)"),
            text("lower(name)"),
            unique=True,
        ),
        Index(
            "uq_repo_connections_one_default_per_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    provider: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    is_default: Mapped[bool] = mapped_column(server_default=text("false"))


class Member(IdMixin, TimestampsMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "members"
    __table_args__ = (
        CheckConstraint(one_of("role", Role), name="role"),
        # Target of the composite foreign key from identities (same-workspace guarantee).
        UniqueConstraint("workspace_id", "id"),
    )

    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default=Role.MEMBER.value)


class Identity(IdMixin, TimestampsMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "external_id"),
        UniqueConstraint("member_id", "platform"),  # one account per platform per member
        CheckConstraint(one_of("kind", IdentityKind), name="kind"),
        CheckConstraint(
            f"kind <> '{IdentityKind.CODEHOST}' OR username IS NOT NULL", name="codehost_login"
        ),
        # The member must belong to the same workspace as the identity.
        ForeignKeyConstraint(
            ["workspace_id", "member_id"],
            ["members.workspace_id", "members.id"],
            ondelete="CASCADE",
        ),
    )

    member_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(server_default=text("false"))


class ProcessedEvent(IdMixin, Base):
    """A delivery is processed only if inserting its row succeeds (idempotency)."""

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class BufferedMessage(IdMixin, WorkspaceScopedMixin, Base):
    """A recent chat message kept as context for the LLM; the text is encrypted (D46)."""

    __tablename__ = "message_buffer"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "chat_id", "message_id"),
        Index(
            "ix_message_buffer_workspace_id_chat_sent_at",
            "workspace_id",
            "platform",
            "chat_id",
            "sent_at",
        ),
    )

    platform: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)  # a forum topic; context stays inside it
    message_id: Mapped[str] = mapped_column(Text)
    reply_to_message_id: Mapped[str | None] = mapped_column(Text)
    sender_id: Mapped[str] = mapped_column(Text)
    sender_name: Mapped[str] = mapped_column(Text)
    sender_username: Mapped[str | None] = mapped_column(Text)
    text_encrypted: Mapped[str] = mapped_column(Text)
    # JSON of the files sent with it (id, type, size; never the bytes), encrypted like the text.
    attachments_encrypted: Mapped[str | None] = mapped_column(Text)
    # sent_at orders messages (never later than received); received_at, set by the database,
    # bounds how long anything is kept whatever date a platform reports. Both serve pruning.
    sent_at: Mapped[datetime] = mapped_column(index=True)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class AuditEvent(IdMixin, Base):
    """Append-only. workspace_id is nullable: requests from unlinked chats are audited too."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(one_of("result", AuditResult), name="result"),
        Index("ix_audit_events_workspace_id_created_at", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    actor_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL")
    )
    confirmed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)
    result: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Secret(IdMixin, TimestampsMixin, Base):
    """Encrypted value, global (workspace_id NULL) or per workspace."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("workspace_id", "name", postgresql_nulls_not_distinct=True),)

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[str] = mapped_column(Text)


class PollCursor(IdMixin, Base):
    __tablename__ = "poll_cursors"
    __table_args__ = (UniqueConstraint("source", "scope"),)

    source: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    cursor: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
