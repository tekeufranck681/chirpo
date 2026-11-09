"""Database rules the app relies on, enforced by Postgres itself (not only by Python code)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chirpo.db.models import (
    AuditEvent,
    Identity,
    Member,
    ProcessedEvent,
    RepoConnection,
    Secret,
    Workspace,
)

pytestmark = pytest.mark.integration


async def _workspace(session: AsyncSession, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name)
    session.add(workspace)
    await session.flush()
    return workspace


async def test_defaults_are_filled_by_the_database(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    member = Member(workspace_id=workspace.id, display_name="Annie")
    db_session.add(member)
    await db_session.flush()
    await db_session.refresh(workspace)
    await db_session.refresh(member)

    assert isinstance(workspace.id, uuid.UUID)
    assert workspace.settings == {}
    assert workspace.created_at.tzinfo is not None
    assert member.role == "member"


async def test_duplicate_event_delivery_is_rejected(db_session: AsyncSession) -> None:
    # CLAUDE.md rule 6: the same Telegram update must never be processed twice.
    db_session.add(ProcessedEvent(source="telegram", external_id="42"))
    await db_session.flush()
    db_session.add(ProcessedEvent(source="telegram", external_id="42"))
    with pytest.raises(IntegrityError, match="uq_processed_events_source_external_id"):
        await db_session.flush()


async def test_same_external_id_from_another_source_is_fine(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            ProcessedEvent(source="telegram", external_id="42"),
            ProcessedEvent(source="github", external_id="42"),
        ]
    )
    await db_session.flush()


async def test_unknown_role_is_rejected(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    db_session.add(Member(workspace_id=workspace.id, display_name="Eve", role="superuser"))
    with pytest.raises(IntegrityError, match="ck_members_role"):
        await db_session.flush()


async def test_only_one_default_repo_per_workspace(db_session: AsyncSession) -> None:
    workspace = await _workspace(db_session)
    db_session.add(
        RepoConnection(
            workspace_id=workspace.id, provider="github", owner="acme", name="app", is_default=True
        )
    )
    await db_session.flush()
    db_session.add(
        RepoConnection(
            workspace_id=workspace.id, provider="github", owner="acme", name="api", is_default=True
        )
    )
    with pytest.raises(IntegrityError, match="uq_repo_connections_one_default_per_workspace"):
        await db_session.flush()


async def test_global_secret_names_are_unique(db_session: AsyncSession) -> None:
    # workspace_id is NULL for global secrets; NULLS NOT DISTINCT still enforces uniqueness.
    db_session.add(Secret(name="telegram_bot_token", ciphertext="a"))
    await db_session.flush()
    db_session.add(Secret(name="telegram_bot_token", ciphertext="b"))
    with pytest.raises(IntegrityError, match="uq_secrets_workspace_id_name"):
        await db_session.flush()


async def test_deleting_a_workspace_removes_its_rows_but_keeps_the_audit_log(
    db_session: AsyncSession,
) -> None:
    workspace = await _workspace(db_session)
    member = Member(workspace_id=workspace.id, display_name="Annie")
    db_session.add(member)
    await db_session.flush()
    audit = AuditEvent(
        workspace_id=workspace.id,
        actor_member_id=member.id,
        action="create_issue",
        result="executed",
    )
    db_session.add(audit)
    await db_session.flush()

    await db_session.delete(workspace)
    await db_session.flush()
    db_session.expunge_all()

    assert (await db_session.scalars(select(Member))).all() == []
    kept = await db_session.get(AuditEvent, audit.id)
    assert kept is not None
    assert kept.workspace_id is None
    assert kept.actor_member_id is None


async def test_unknown_audit_result_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(AuditEvent(action="create_issue", result="maybe"))
    with pytest.raises(IntegrityError, match="ck_audit_events_result"):
        await db_session.flush()


async def test_identity_cannot_point_to_a_member_of_another_workspace(
    db_session: AsyncSession,
) -> None:
    team_a, team_b = await _workspace(db_session, "A"), await _workspace(db_session, "B")
    member_of_b = Member(workspace_id=team_b.id, display_name="Bob")
    db_session.add(member_of_b)
    await db_session.flush()
    db_session.add(
        Identity(
            workspace_id=team_a.id,
            member_id=member_of_b.id,
            kind="chat",
            platform="telegram",
            external_id="777",
        )
    )
    with pytest.raises(IntegrityError, match="fk_identities_workspace_id_members"):
        await db_session.flush()
