"""Implementations of the repository Protocols from core.ports."""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, bindparam, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chirpo.core.domain import (
    Attachment,
    ChatRef,
    ChatUser,
    IdentityKind,
    InboundMessage,
    RecentMessage,
    RepoRef,
)
from chirpo.core.errors import LastAdminError
from chirpo.core.languages import LANGUAGES
from chirpo.core.llm import MAX_MESSAGE_CHARS
from chirpo.core.pending import FINISHED, RUNNING, PendingAction, PendingStatus, Step
from chirpo.core.permissions import Role, role_change_refusal
from chirpo.core.statuses import STATUSES, StatusLabels, label_problem
from chirpo.core.summary import parse_time, timezone_named
from chirpo.core.workspaces import (
    DEFAULT_PERSONALITY,
    PERSONALITIES,
    AuditEntry,
    CodeHostAccount,
    LinkedRepo,
    Member,
    Personality,
    ScheduledSummary,
    SummarySchedule,
    Workspace,
)
from chirpo.db import models as db
from chirpo.db.models import PollCursor, ProcessedEvent
from chirpo.security import DecryptionError, Encryptor


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

    async def release(self, source: str, external_id: str) -> None:
        statement = delete(ProcessedEvent).where(
            ProcessedEvent.source == source, ProcessedEvent.external_id == external_id
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def prune(self, older_than_days: int) -> int:
        cutoff = func.now() - timedelta(days=older_than_days)
        statement = delete(ProcessedEvent).where(ProcessedEvent.received_at < cutoff)
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
            return int(result.rowcount)  # type: ignore[attr-defined]


# Keys in workspaces.settings
KEEP_CONTEXT = "keep_context"
PERSONALITY = "personality"
STATUS_LABELS = "status_labels"
ISSUE_LANGUAGE = "issue_language"
TIMEZONE = "timezone"
# Keys in chat_connections.notify_prefs
SUMMARY = "summary"  # {"at": "09:00", "weekdays": false}
SUMMARY_SENT_ON = "summary_sent_on"  # the team's local day, "2026-11-13"


MAX_KEPT_ATTACHMENTS = 10


class SqlMessageBuffer:
    """Recent chat messages, text encrypted (D46).

    A message is read while both its sent and received times are within the retention, and pruned
    as soon as either is older: a wrong date from a platform can't keep it around.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], encryptor: Encryptor, retention: timedelta
    ) -> None:
        self._sessions = sessions
        self._encryptor = encryptor
        self._retention = retention

    async def remember(self, workspace_id: UUID, message: InboundMessage) -> None:
        statement = (
            insert(db.BufferedMessage)
            .values(
                workspace_id=workspace_id,
                platform=message.chat.platform,
                chat_id=message.chat.chat_id,
                thread_id=message.chat.thread_id,
                message_id=message.message_id,
                reply_to_message_id=message.reply_to_message_id,
                sender_id=message.sender.user_id,
                sender_name=message.sender.display_name,
                sender_username=message.sender.username,
                text_encrypted=self._encryptor.encrypt(message.text[:MAX_MESSAGE_CHARS]),
                attachments_encrypted=self._seal(message.attachments),
                sent_at=min(message.sent_at, datetime.now(UTC)),
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "platform", "chat_id", "message_id"]
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def recent(
        self, workspace_id: UUID, chat: ChatRef, *, limit: int = 20
    ) -> list[RecentMessage]:
        if limit <= 0:
            return []
        cutoff = func.now() - self._retention
        statement = (
            select(db.BufferedMessage)
            .where(
                db.BufferedMessage.workspace_id == workspace_id,
                db.BufferedMessage.platform == chat.platform,
                db.BufferedMessage.chat_id == chat.chat_id,
                db.BufferedMessage.thread_id.is_not_distinct_from(chat.thread_id),
                db.BufferedMessage.sent_at >= cutoff,
                db.BufferedMessage.received_at >= cutoff,
            )
            .order_by(db.BufferedMessage.sent_at.desc(), db.BufferedMessage.received_at.desc())
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).scalars().all()
        recent = (self._to_recent(row) for row in reversed(rows))
        return [message for message in recent if message is not None]

    async def prune(self) -> int:
        cutoff = func.now() - self._retention
        statement = delete(db.BufferedMessage).where(
            or_(db.BufferedMessage.sent_at < cutoff, db.BufferedMessage.received_at < cutoff)
        )
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
            return int(result.rowcount)  # type: ignore[attr-defined]

    async def find(
        self, workspace_id: UUID, chat: ChatRef, message_id: str
    ) -> RecentMessage | None:
        cutoff = func.now() - self._retention
        statement = select(db.BufferedMessage).where(
            db.BufferedMessage.workspace_id == workspace_id,
            db.BufferedMessage.platform == chat.platform,
            db.BufferedMessage.chat_id == chat.chat_id,
            db.BufferedMessage.thread_id.is_not_distinct_from(chat.thread_id),
            db.BufferedMessage.message_id == message_id,
            db.BufferedMessage.sent_at >= cutoff,
            db.BufferedMessage.received_at >= cutoff,
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
        return self._to_recent(row) if row else None

    async def forget(self, workspace_id: UUID) -> int:
        statement = delete(db.BufferedMessage).where(
            db.BufferedMessage.workspace_id == workspace_id
        )
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
            return int(result.rowcount)  # type: ignore[attr-defined]

    def _seal(self, attachments: tuple[Attachment, ...]) -> str | None:
        if not attachments:
            return None
        listed = [a.model_dump(mode="json") for a in attachments[:MAX_KEPT_ATTACHMENTS]]
        return self._encryptor.encrypt(json.dumps(listed, separators=(",", ":")))

    def _unseal(self, token: str | None) -> tuple[Attachment, ...]:
        if token is None:
            return ()
        try:
            listed = json.loads(self._encryptor.decrypt(token))
            return tuple(Attachment.model_validate(item) for item in listed)
        except (DecryptionError, ValueError, TypeError):
            return ()  # the text is still useful context without its files

    def _to_recent(self, row: db.BufferedMessage) -> RecentMessage | None:
        try:
            text = self._encryptor.decrypt(row.text_encrypted)
        except DecryptionError:
            return None  # written with a key since removed: context is best effort
        return RecentMessage(
            message_id=row.message_id,
            reply_to_message_id=row.reply_to_message_id,
            sender=ChatUser(
                platform=row.platform,
                user_id=row.sender_id,
                display_name=row.sender_name,
                username=row.sender_username,
            ),
            text=text,
            sent_at=row.sent_at,
            attachments=self._unseal(row.attachments_encrypted),
        )


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
    """Settings are read defensively: a value that isn't valid any more falls back to the
    default rather than break every request of the workspace."""
    stored = row.settings.get(PERSONALITY)
    language = row.settings.get(ISSUE_LANGUAGE)
    zone = row.settings.get(TIMEZONE)
    return Workspace(
        id=row.id,
        name=row.name,
        keeps_context=row.settings.get(KEEP_CONTEXT, True) is not False,
        personality=stored if stored in PERSONALITIES else DEFAULT_PERSONALITY,
        statuses=_status_labels(row.settings.get(STATUS_LABELS)),
        issue_language=language if language in LANGUAGES else None,
        timezone=zone if isinstance(zone, str) and timezone_named(zone) == zone else "UTC",
    )


def _to_schedule(prefs: dict[str, Any]) -> SummarySchedule | None:
    stored = prefs.get(SUMMARY)
    if not isinstance(stored, dict) or not isinstance(stored.get("at"), str):
        return None
    at = parse_time(stored["at"])
    return SummarySchedule(at=at, weekdays_only=stored.get("weekdays") is True) if at else None


def _sent_on(prefs: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(prefs[SUMMARY_SENT_ON])
    except (KeyError, TypeError, ValueError):
        return None


def _same_chat(chat: ChatRef) -> tuple[ColumnElement[bool], ...]:
    return (
        db.ChatConnection.platform == chat.platform,
        db.ChatConnection.external_chat_id == chat.chat_id,
    )


def _status_labels(stored: object) -> StatusLabels:
    labels = StatusLabels()
    if not isinstance(stored, dict):
        return labels
    for status in STATUSES:
        name = stored.get(status)
        if isinstance(name, str) and label_problem(name, labels, status) is None:
            labels = labels.with_label(status, name)
    return labels


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

    async def set_keeps_context(self, workspace_id: UUID, *, keep: bool) -> None:
        await self._merge_settings(workspace_id, {KEEP_CONTEXT: keep})

    async def set_personality(self, workspace_id: UUID, personality: Personality) -> None:
        await self._merge_settings(workspace_id, {PERSONALITY: personality})

    async def set_status_labels(self, workspace_id: UUID, labels: StatusLabels) -> None:
        stored = {status: labels.label(status) for status in STATUSES}
        await self._merge_settings(workspace_id, {STATUS_LABELS: stored})

    async def set_issue_language(self, workspace_id: UUID, code: str | None) -> None:
        await self._merge_settings(workspace_id, {ISSUE_LANGUAGE: code})

    async def _merge_settings(self, workspace_id: UUID, values: dict[str, Any]) -> None:
        """Replaces only these top-level keys, in one statement: two admins changing different
        settings at the same moment both keep their change. The four status labels are one key,
        so two renames at the same instant keep the last (and a duplicate is dropped on read)."""
        statement = (
            update(db.Workspace)
            .where(db.Workspace.id == workspace_id)
            .values(settings=db.Workspace.settings.op("||")(bindparam("values", values, JSONB)))
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def set_timezone(self, workspace_id: UUID, name: str) -> None:
        await self._merge_settings(workspace_id, {TIMEZONE: name})

    async def summary_schedule(self, chat: ChatRef) -> SummarySchedule | None:
        statement = select(db.ChatConnection.notify_prefs).where(*_same_chat(chat))
        async with self._sessions() as session:
            prefs = await session.scalar(statement)
        return _to_schedule(prefs) if prefs else None

    async def set_summary_schedule(self, chat: ChatRef, schedule: SummarySchedule | None) -> None:
        prefs = db.ChatConnection.notify_prefs
        if schedule is None:  # the key goes, so the scheduler no longer reads this chat
            changed = prefs.op("-")(SUMMARY)
        else:
            value = {"at": schedule.at.strftime("%H:%M"), "weekdays": schedule.weekdays_only}
            changed = prefs.op("||")(bindparam("prefs", {SUMMARY: value}, JSONB))
        statement = update(db.ChatConnection).where(*_same_chat(chat)).values(notify_prefs=changed)
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def scheduled_summaries(self) -> list[ScheduledSummary]:
        # Across all workspaces on purpose (the scheduler's only query without a workspace_id):
        # each summary is then built only from its own chat's workspace.
        statement = (
            select(db.ChatConnection, db.Workspace)
            .join(db.Workspace, db.Workspace.id == db.ChatConnection.workspace_id)
            .where(db.ChatConnection.notify_prefs.has_key(SUMMARY))
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).all()
        found = []
        for chat, workspace in rows:
            schedule = _to_schedule(chat.notify_prefs)
            if schedule is not None:
                found.append(
                    ScheduledSummary(
                        chat=ChatRef(platform=chat.platform, chat_id=chat.external_chat_id),
                        workspace=_to_workspace(workspace),
                        schedule=schedule,
                        sent_on=_sent_on(chat.notify_prefs),
                    )
                )
        return found

    async def claim_summary(self, chat: ChatRef, day: date) -> bool:
        """One statement: of two workers claiming the same day, only one changes the row."""
        sent_on = db.ChatConnection.notify_prefs[SUMMARY_SENT_ON].astext
        statement = (
            update(db.ChatConnection)
            .where(*_same_chat(chat), sent_on.is_distinct_from(day.isoformat()))
            .values(
                notify_prefs=db.ChatConnection.notify_prefs.op("||")(
                    bindparam("sent", {SUMMARY_SENT_ON: day.isoformat()}, JSONB)
                )
            )
            .returning(db.ChatConnection.id)
        )
        async with self._sessions.begin() as session:
            return (await session.scalar(statement)) is not None

    async def release_summary(self, chat: ChatRef, day: date, previous: date | None) -> None:
        sent_on = db.ChatConnection.notify_prefs[SUMMARY_SENT_ON].astext
        value = previous.isoformat() if previous else None
        statement = (
            update(db.ChatConnection)
            .where(*_same_chat(chat), sent_on == day.isoformat())
            .values(
                notify_prefs=db.ChatConnection.notify_prefs.op("||")(
                    bindparam("sent", {SUMMARY_SENT_ON: value}, JSONB)
                )
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def set_default_repo(self, workspace_id: UUID, repo: RepoRef) -> bool:
        async with self._sessions.begin() as session:
            await _lock_workspace(session, workspace_id)
            in_workspace = db.RepoConnection.workspace_id == workspace_id
            row = await session.scalar(
                select(db.RepoConnection).where(in_workspace, *_same_repo(repo))
            )
            if row is None:
                return False
            # The old default goes first: one default per workspace is a unique index.
            await session.execute(
                update(db.RepoConnection)
                .where(in_workspace, db.RepoConnection.id != row.id, db.RepoConnection.is_default)
                .values(is_default=False)
            )
            await session.execute(
                update(db.RepoConnection)
                .where(db.RepoConnection.id == row.id)
                .values(is_default=True)
            )
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


class SqlPendingActions:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        workspace_id: UUID,
        requested_by: UUID,
        chat: ChatRef,
        steps: Sequence[Step],
        expires_at: datetime,
    ) -> PendingAction:
        async with self._sessions.begin() as session:
            row = db.PendingAction(
                workspace_id=workspace_id,
                requested_by=requested_by,
                platform=chat.platform,
                chat_id=chat.chat_id,
                thread_id=chat.thread_id,
                steps=[step.model_dump(mode="json") for step in steps],
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)  # the id and defaults the database chose
            return _to_pending(row)

    async def attach_message(self, workspace_id: UUID, pending_id: UUID, message_id: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(db.PendingAction)
                .where(
                    db.PendingAction.id == pending_id,
                    db.PendingAction.workspace_id == workspace_id,
                )
                .values(message_id=message_id)
            )

    async def get(self, workspace_id: UUID, pending_id: UUID) -> PendingAction | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(db.PendingAction).where(
                    db.PendingAction.id == pending_id,
                    db.PendingAction.workspace_id == workspace_id,
                )
            )
            return _to_pending(row) if row is not None else None

    async def find_by_card(
        self, workspace_id: UUID, chat: ChatRef, message_id: str
    ) -> PendingAction | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(db.PendingAction).where(
                    db.PendingAction.workspace_id == workspace_id,
                    db.PendingAction.platform == chat.platform,
                    db.PendingAction.chat_id == chat.chat_id,
                    db.PendingAction.thread_id.is_not_distinct_from(chat.thread_id),
                    db.PendingAction.message_id == message_id,
                )
            )
            return _to_pending(row) if row is not None else None

    async def expire_due(self, now: datetime) -> list[PendingAction]:
        # One UPDATE: each row is expired by exactly one sweep, and never one that was just
        # confirmed (that no longer matches status = 'proposed').
        return await self._sweep(
            db.PendingAction.status == PendingStatus.PROPOSED.value,
            db.PendingAction.expires_at <= now,
            status=PendingStatus.EXPIRED.value,
        )

    async def fail_stuck(self, before: datetime) -> list[PendingAction]:
        return await self._sweep(
            db.PendingAction.status.in_([status.value for status in RUNNING]),
            db.PendingAction.updated_at < before,
            status=PendingStatus.FAILED.value,
            result={"error": "stuck"},
        )

    async def purge(self, before: datetime) -> int:
        async with self._sessions.begin() as session:
            deleted = await session.execute(
                delete(db.PendingAction).where(
                    db.PendingAction.status.in_([status.value for status in FINISHED]),
                    db.PendingAction.updated_at < before,
                )
            )
            return deleted.rowcount  # type: ignore[attr-defined,no-any-return]

    async def _sweep(self, *conditions: ColumnElement[bool], **values: Any) -> list[PendingAction]:
        async with self._sessions.begin() as session:
            rows = await session.scalars(
                update(db.PendingAction)
                .where(*conditions)
                .values(**values)
                .returning(db.PendingAction)
            )
            return [_to_pending(row) for row in rows]

    async def advance(
        self,
        workspace_id: UUID,
        pending_id: UUID,
        *,
        expect: PendingStatus,
        to: PendingStatus,
        now: datetime,
        by: UUID | None = None,
        result: dict[str, Any] | None = None,
    ) -> PendingAction | None:
        # One UPDATE decides the race: only a row still in `expect` (and, when it is being
        # decided, not yet expired) matches, so of two simultaneous presses one gets the row.
        conditions: list[ColumnElement[bool]] = [
            db.PendingAction.id == pending_id,
            db.PendingAction.workspace_id == workspace_id,
            db.PendingAction.status == expect.value,
        ]
        if expect == PendingStatus.PROPOSED and to != PendingStatus.EXPIRED:
            conditions.append(db.PendingAction.expires_at > now)
        values: dict[str, Any] = {"status": to.value}
        if by is not None:
            values["confirmed_by"] = by
        if result is not None:
            values["result"] = result
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(db.PendingAction)
                    .where(*conditions)
                    .values(**values)
                    .returning(db.PendingAction)
                )
            ).scalar_one_or_none()
            return _to_pending(row) if row is not None else None


def _to_pending(row: db.PendingAction) -> PendingAction:
    return PendingAction(
        id=row.id,
        workspace_id=row.workspace_id,
        requested_by=row.requested_by,
        chat=ChatRef(platform=row.platform, chat_id=row.chat_id, thread_id=row.thread_id),
        message_id=row.message_id,
        steps=tuple(Step.model_validate(step) for step in row.steps),
        status=PendingStatus(row.status),
        confirmed_by=row.confirmed_by,
        expires_at=row.expires_at,
        updated_at=row.updated_at,
        result=row.result,
    )
