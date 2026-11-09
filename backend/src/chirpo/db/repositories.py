"""Implementations of the repository Protocols from core.ports."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chirpo.core.domain import ChatRef, ChatUser, IdentityKind, RepoRef
from chirpo.core.errors import LastAdminError
from chirpo.core.permissions import Role, role_change_refusal
from chirpo.core.workspaces import AuditEntry, CodeHostAccount, LinkedRepo, Member, Workspace
from chirpo.db import models as db
from chirpo.db.models import PollCursor, ProcessedEvent


class SqlProcessedEvents:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(self, source: str, external_id: str) -> bool:
        statement = (
            insert(ProcessedEvent)
            .values(source=source, external_id=external_id)
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
            .returning(ProcessedEvent.id)
        )
        async with self._sessions.begin() as session:
            return (await session.execute(statement)).scalar_one_or_none() is not None

    async def prune(self, older_than_days: int) -> int:
        cutoff = func.now() - timedelta(days=older_than_days)
        statement = delete(ProcessedEvent).where(ProcessedEvent.received_at < cutoff)
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
            return int(result.rowcount)  # type: ignore[attr-defined]


class SqlPollCursors:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, source: str, scope: str) -> str | None:
        statement = select(PollCursor.cursor).where(
            PollCursor.source == source, PollCursor.scope == scope
        )
        async with self._sessions() as session:
            return (await session.execute(statement)).scalar_one_or_none()

    async def set(self, source: str, scope: str, cursor: str) -> None:
        statement = (
            insert(PollCursor)
            .values(source=source, scope=scope, cursor=cursor)
            .on_conflict_do_update(
                index_elements=["source", "scope"],
                set_={"cursor": cursor, "updated_at": func.now()},
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)


class _LostRace(Exception):
    """Another request created the same row first; re-read instead."""


def _winner[T](found: T | None) -> T:
    """After losing a creation race, the winner's committed row must be readable."""
    if found is None:
        raise RuntimeError("row missing after a lost creation race")
    return found


def _to_workspace(row: db.Workspace) -> Workspace:
    return Workspace(id=row.id, name=row.name)


def _to_linked_repo(row: db.RepoConnection) -> LinkedRepo:
    return LinkedRepo(
        repo=RepoRef(provider=row.provider, owner=row.owner, name=row.name),
        is_default=row.is_default,
        installation_id=row.installation_id,
    )


def _same_repo(repo: RepoRef) -> list[ColumnElement[bool]]:
    return [
        db.RepoConnection.provider == repo.provider,
        func.lower(db.RepoConnection.owner) == repo.owner.lower(),
        func.lower(db.RepoConnection.name) == repo.name.lower(),
    ]


async def _lock_workspace(session: AsyncSession, workspace_id: UUID) -> None:
    """Serialise changes to one workspace's repo links (the default flag must stay unique)."""
    await session.execute(
        select(db.Workspace.id)
        .where(db.Workspace.id == workspace_id)
        # NO KEY UPDATE: serialises link changes without blocking inserts that reference the row.
        .with_for_update(key_share=True)
    )


class SqlWorkspaces:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def for_chat(self, chat: ChatRef) -> Workspace | None:
        statement = (
            select(db.Workspace)
            .join(db.ChatConnection, db.ChatConnection.workspace_id == db.Workspace.id)
            .where(
                db.ChatConnection.platform == chat.platform,
                db.ChatConnection.external_chat_id == chat.chat_id,
            )
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
            return _to_workspace(row) if row else None

    async def get_or_create_for_chat(self, chat: ChatRef, *, name: str) -> Workspace:
        if found := await self.for_chat(chat):
            return found
        try:
            async with self._sessions.begin() as session:
                workspace = db.Workspace(name=name)
                session.add(workspace)
                await session.flush()
                linked = await session.execute(
                    insert(db.ChatConnection)
                    .values(
                        workspace_id=workspace.id,
                        platform=chat.platform,
                        external_chat_id=chat.chat_id,
                        title=name,
                    )
                    .on_conflict_do_nothing(index_elements=["platform", "external_chat_id"])
                    .returning(db.ChatConnection.id)
                )
                if linked.scalar_one_or_none() is None:
                    raise _LostRace
                return _to_workspace(workspace)
        except _LostRace:
            return _winner(await self.for_chat(chat))

    async def repos(self, workspace_id: UUID) -> list[LinkedRepo]:
        statement = (
            select(db.RepoConnection)
            .where(db.RepoConnection.workspace_id == workspace_id)
            .order_by(
                db.RepoConnection.is_default.desc(),
                db.RepoConnection.created_at,
                db.RepoConnection.id,
            )
        )
        async with self._sessions() as session:
            return [_to_linked_repo(row) for row in (await session.execute(statement)).scalars()]

    async def link_repo(
        self,
        workspace_id: UUID,
        repo: RepoRef,
        *,
        installation_id: int | None = None,
        first_admin: UUID | None = None,
    ) -> LinkedRepo | None:
        async with self._sessions.begin() as session:
            await _lock_workspace(session, workspace_id)
            in_workspace = db.RepoConnection.workspace_id == workspace_id
            if first_admin is not None:
                if await session.scalar(select(func.count()).where(in_workspace)):
                    return None
                await session.execute(
                    update(db.Member)
                    .where(db.Member.workspace_id == workspace_id, db.Member.id == first_admin)
                    .values(role=Role.ADMIN)
                )
            existing = await session.scalar(
                select(db.RepoConnection).where(in_workspace, *_same_repo(repo))
            )
            if existing:
                if installation_id is not None:
                    existing.installation_id = installation_id  # the App may have been reinstalled
                return _to_linked_repo(existing)
            has_default = await session.scalar(
                select(func.count()).where(in_workspace, db.RepoConnection.is_default)
            )
            row = db.RepoConnection(
                workspace_id=workspace_id,
                provider=repo.provider,
                owner=repo.owner,
                name=repo.name,
                installation_id=installation_id,
                is_default=not has_default,
            )
            session.add(row)
            await session.flush()
            return _to_linked_repo(row)

    async def unlink_repo(self, workspace_id: UUID, repo: RepoRef) -> bool:
        async with self._sessions.begin() as session:
            await _lock_workspace(session, workspace_id)
            in_workspace = db.RepoConnection.workspace_id == workspace_id
            row = await session.scalar(
                select(db.RepoConnection).where(in_workspace, *_same_repo(repo))
            )
            if row is None:
                return False
            was_default = row.is_default
            await session.delete(row)
            await session.flush()
            if was_default:
                oldest = await session.scalar(
                    select(db.RepoConnection)
                    .where(in_workspace)
                    .order_by(db.RepoConnection.created_at, db.RepoConnection.id)
                    .limit(1)
                )
                if oldest:
                    oldest.is_default = True
            return True


class SqlMembers:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def for_chat_user(self, workspace_id: UUID, user: ChatUser) -> Member:
        if member := await self._find_by_chat_account(workspace_id, user):
            if (member.display_name, member.chat_username) != (user.display_name, user.username):
                return await self._rename(member, user)
            return member
        try:
            async with self._sessions.begin() as session:
                member_id = await session.scalar(
                    insert(db.Member)
                    .values(workspace_id=workspace_id, display_name=user.display_name)
                    .returning(db.Member.id)
                )
                linked = await session.scalar(
                    insert(db.Identity)
                    .values(
                        workspace_id=workspace_id,
                        member_id=member_id,
                        kind=IdentityKind.CHAT,
                        platform=user.platform,
                        external_id=user.user_id,
                        username=user.username,
                        verified=True,  # the chat platform vouches for its own user ids
                    )
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "platform", "external_id"]
                    )
                    .returning(db.Identity.id)
                )
                if linked is None or member_id is None:
                    raise _LostRace
                (member,) = await _load_members(session, workspace_id, [member_id])
                return member
        except _LostRace:
            return _winner(await self._find_by_chat_account(workspace_id, user))

    async def all(self, workspace_id: UUID) -> list[Member]:
        async with self._sessions() as session:
            ids = (
                await session.scalars(
                    select(db.Member.id)
                    .where(db.Member.workspace_id == workspace_id)
                    .order_by(db.Member.created_at, db.Member.id)
                )
            ).all()
            return await _load_members(session, workspace_id, list(ids))

    async def set_role(self, workspace_id: UUID, member_id: UUID, role: Role) -> Member | None:
        async with self._sessions.begin() as session:
            await _lock_workspace(session, workspace_id)  # two admins demoting each other at once
            target = await _load_members(session, workspace_id, [member_id])
            if not target:
                return None
            admins = await session.scalar(
                select(func.count()).where(
                    db.Member.workspace_id == workspace_id, db.Member.role == Role.ADMIN
                )
            )
            if role_change_refusal(target[0], role, admins or 0):
                raise LastAdminError
            updated = await session.scalar(
                update(db.Member)
                .where(db.Member.workspace_id == workspace_id, db.Member.id == member_id)
                .values(role=role)
                .returning(db.Member.id)
            )
            if updated is None:
                return None
            (member,) = await _load_members(session, workspace_id, [updated])
            return member

    async def link_codehost(
        self, workspace_id: UUID, member_id: UUID, account: CodeHostAccount
    ) -> Member | None:
        async with self._sessions.begin() as session:
            # One account can be claimed by two people at once: serialise per workspace.
            await _lock_workspace(session, workspace_id)
            exists = await session.scalar(
                select(db.Member.id).where(
                    db.Member.workspace_id == workspace_id, db.Member.id == member_id
                )
            )
            if exists is None:
                return None
            await session.execute(
                delete(db.Identity).where(
                    db.Identity.workspace_id == workspace_id,
                    db.Identity.kind == IdentityKind.CODEHOST,
                    db.Identity.platform == account.provider,
                    or_(
                        db.Identity.member_id == member_id,
                        db.Identity.external_id == account.user_id,
                    ),
                )
            )
            session.add(
                db.Identity(
                    workspace_id=workspace_id,
                    member_id=member_id,
                    kind=IdentityKind.CODEHOST,
                    platform=account.provider,
                    external_id=account.user_id,
                    username=account.login,
                    verified=True,
                )
            )
            await session.flush()
            (member,) = await _load_members(session, workspace_id, [member_id])
            return member

    async def _find_by_chat_account(self, workspace_id: UUID, user: ChatUser) -> Member | None:
        async with self._sessions() as session:
            member_id = await session.scalar(
                select(db.Identity.member_id).where(
                    db.Identity.workspace_id == workspace_id,
                    db.Identity.platform == user.platform,
                    db.Identity.external_id == user.user_id,
                )
            )
            if member_id is None:
                return None
            (member,) = await _load_members(session, workspace_id, [member_id])
            return member

    async def _rename(self, member: Member, user: ChatUser) -> Member:
        async with self._sessions.begin() as session:
            await session.execute(
                update(db.Member)
                .where(db.Member.workspace_id == member.workspace_id, db.Member.id == member.id)
                .values(display_name=user.display_name)
            )
            await session.execute(
                update(db.Identity)
                .where(
                    db.Identity.workspace_id == member.workspace_id,
                    db.Identity.member_id == member.id,
                    db.Identity.kind == IdentityKind.CHAT,
                    db.Identity.platform == user.platform,
                )
                .values(username=user.username)
            )
            (renamed,) = await _load_members(session, member.workspace_id, [member.id])
            return renamed


async def _load_members(
    session: AsyncSession, workspace_id: UUID, member_ids: list[UUID]
) -> list[Member]:
    if not member_ids:
        return []
    rows = (
        await session.scalars(
            select(db.Member).where(
                db.Member.workspace_id == workspace_id, db.Member.id.in_(member_ids)
            )
        )
    ).all()
    identities = (
        await session.scalars(
            select(db.Identity)
            .where(db.Identity.workspace_id == workspace_id, db.Identity.member_id.in_(member_ids))
            .order_by(db.Identity.created_at, db.Identity.id)  # latest account per kind wins
        )
    ).all()
    chat_names: dict[UUID, str | None] = {}
    accounts: dict[UUID, CodeHostAccount] = {}
    for identity in identities:
        if identity.kind == IdentityKind.CHAT:
            chat_names[identity.member_id] = identity.username
        elif identity.verified and identity.username:  # a DB check guarantees the login
            accounts[identity.member_id] = CodeHostAccount(
                provider=identity.platform,
                user_id=identity.external_id,
                login=identity.username,
            )
    by_id = {
        row.id: Member(
            id=row.id,
            workspace_id=row.workspace_id,
            display_name=row.display_name,
            role=Role(row.role),
            chat_username=chat_names.get(row.id),
            codehost=accounts.get(row.id),
        )
        for row in rows
    }
    return [by_id[member_id] for member_id in member_ids if member_id in by_id]


class SqlAuditLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, entry: AuditEntry) -> None:
        async with self._sessions.begin() as session:
            session.add(
                db.AuditEvent(
                    workspace_id=entry.workspace_id,
                    actor_member_id=entry.actor_member_id,
                    confirmed_by_member_id=entry.confirmed_by_member_id,
                    action=entry.action,
                    payload=entry.payload,
                    result=entry.result,
                    error=entry.error,
                )
            )
