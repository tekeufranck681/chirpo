"""Pending actions: the same tests run on the in-memory fake and on PostgreSQL, so core tests that
use the fake see the real behaviour (M5.1)."""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from chirpo.core.actions import CloseIssue, IssueRef
from chirpo.core.domain import ChatRef, ChatUser, RepoRef
from chirpo.core.pending import PendingAction, PendingStatus, Step
from chirpo.core.ports import Members, PendingActions, Workspaces
from chirpo.core.workspaces import Member, Workspace
from chirpo.db.repositories import SqlMembers, SqlPendingActions, SqlWorkspaces
from chirpo.db.session import create_session_factory
from tests.fakes import FakeMembers, FakePendingActions, FakeWorkspaces

REPO = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
STEP = Step.of(CloseIssue(repo=REPO.full_name, issue=IssueRef(number=7)), REPO)
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@dataclass
class Stores:
    workspaces: Workspaces
    members: Members
    pending: PendingActions
    is_postgres: bool


@pytest.fixture(params=["fake", "postgres"])
def stores(request: pytest.FixtureRequest) -> Iterator[Stores]:
    if request.param == "fake":
        members = FakeMembers()
        yield Stores(FakeWorkspaces(members), members, FakePendingActions(), False)
        return
    engine: AsyncEngine = request.getfixturevalue("engine")
    sessions = create_session_factory(engine)
    yield Stores(SqlWorkspaces(sessions), SqlMembers(sessions), SqlPendingActions(sessions), True)


def new_chat() -> ChatRef:
    return ChatRef(platform="telegram", chat_id=f"-100{uuid.uuid4().int % 10**10}")


async def team(stores: Stores, *, user_id: str = "11") -> tuple[Workspace, Member, ChatRef]:
    chat = new_chat()
    workspace = await stores.workspaces.get_or_create_for_chat(chat, name="Shop")
    user = ChatUser(platform="telegram", user_id=user_id, display_name="Franck")
    return workspace, await stores.members.for_chat_user(workspace.id, user), chat


async def propose(
    stores: Stores, expires_in: timedelta = timedelta(hours=1)
) -> tuple[PendingAction, Workspace, Member]:
    workspace, member, chat = await team(stores)
    row = await stores.pending.create(
        workspace_id=workspace.id,
        requested_by=member.id,
        chat=chat.model_copy(update={"thread_id": "42"}),
        steps=[STEP],
        expires_at=NOW + expires_in,
    )
    return row, workspace, member


async def test_a_new_proposal_waits_for_confirmation(stores: Stores) -> None:
    row, workspace, member = await propose(stores)
    assert (row.status, row.message_id, row.confirmed_by) == (PendingStatus.PROPOSED, None, None)
    assert row.requested_by == member.id
    assert row.chat.thread_id == "42"  # the topic it was asked in survives
    assert row.steps == (STEP,)
    assert row.steps[0].load() == (REPO, CloseIssue(repo=REPO.full_name, issue=IssueRef(number=7)))
    assert row.expires_at == NOW + timedelta(hours=1)
    assert await stores.pending.get(workspace.id, row.id) == row


async def test_a_proposal_is_only_visible_in_its_own_workspace(stores: Stores) -> None:
    row, _, _ = await propose(stores)
    other, _, _ = await team(stores, user_id="12")
    assert await stores.pending.get(other.id, row.id) is None
    assert await stores.pending.get(other.id, uuid.uuid4()) is None
    moved = await stores.pending.advance(
        other.id, row.id, expect=PendingStatus.PROPOSED, to=PendingStatus.CONFIRMED, now=NOW
    )
    assert moved is None


async def test_the_card_message_is_remembered(stores: Stores) -> None:
    row, workspace, _ = await propose(stores)
    await stores.pending.attach_message(workspace.id, row.id, "314")
    found = await stores.pending.get(workspace.id, row.id)
    assert found is not None and found.message_id == "314"


async def test_confirming_records_who_and_moves_on(stores: Stores) -> None:
    row, workspace, member = await propose(stores)
    moved = await stores.pending.advance(
        workspace.id,
        row.id,
        expect=PendingStatus.PROPOSED,
        to=PendingStatus.CONFIRMED,
        now=NOW,
        by=member.id,
    )
    assert moved is not None
    assert (moved.status, moved.confirmed_by) == (PendingStatus.CONFIRMED, member.id)
    assert await stores.pending.get(workspace.id, row.id) == moved


async def test_a_proposal_can_only_be_moved_from_the_state_it_is_in(stores: Stores) -> None:
    row, workspace, _ = await propose(stores)
    first = await stores.pending.advance(
        workspace.id, row.id, expect=PendingStatus.PROPOSED, to=PendingStatus.CONFIRMED, now=NOW
    )
    second = await stores.pending.advance(
        workspace.id, row.id, expect=PendingStatus.PROPOSED, to=PendingStatus.CONFIRMED, now=NOW
    )
    assert first is not None and second is None


async def test_of_many_simultaneous_confirmations_exactly_one_wins(stores: Stores) -> None:
    row, workspace, _ = await propose(stores)
    results = await asyncio.gather(
        *(
            stores.pending.advance(
                workspace.id,
                row.id,
                expect=PendingStatus.PROPOSED,
                to=PendingStatus.CONFIRMED,
                now=NOW,
            )
            for _ in range(8)
        )
    )
    assert sum(result is not None for result in results) == 1


async def test_an_expired_proposal_can_not_be_confirmed_but_can_be_expired(
    stores: Stores,
) -> None:
    row, workspace, _ = await propose(stores, expires_in=timedelta(minutes=5))
    late = NOW + timedelta(minutes=6)
    confirm = await stores.pending.advance(
        workspace.id, row.id, expect=PendingStatus.PROPOSED, to=PendingStatus.CONFIRMED, now=late
    )
    assert confirm is None
    expire = await stores.pending.advance(
        workspace.id, row.id, expect=PendingStatus.PROPOSED, to=PendingStatus.EXPIRED, now=late
    )
    assert expire is not None and expire.status == PendingStatus.EXPIRED


async def test_the_outcome_is_stored(stores: Stores) -> None:
    row, workspace, _ = await propose(stores)
    for expect, to in [
        (PendingStatus.PROPOSED, PendingStatus.CONFIRMED),
        (PendingStatus.CONFIRMED, PendingStatus.EXECUTING),
    ]:
        assert await stores.pending.advance(workspace.id, row.id, expect=expect, to=to, now=NOW)
    done = await stores.pending.advance(
        workspace.id,
        row.id,
        expect=PendingStatus.EXECUTING,
        to=PendingStatus.EXECUTED,
        now=NOW,
        result={"lines": ["Closed #7."], "problem": None},
    )
    assert done is not None
    assert done.result == {"lines": ["Closed #7."], "problem": None}


async def test_a_proposal_for_someone_outside_the_workspace_is_refused(stores: Stores) -> None:
    if not stores.is_postgres:
        pytest.skip("a database guarantee")
    workspace, _, chat = await team(stores)
    _, outsider, _ = await team(stores, user_id="99")  # a member of another workspace
    with pytest.raises(IntegrityError):
        await stores.pending.create(
            workspace_id=workspace.id,
            requested_by=outsider.id,
            chat=chat,
            steps=[STEP],
            expires_at=NOW,
        )
