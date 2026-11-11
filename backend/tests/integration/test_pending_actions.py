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


async def move(
    stores: Stores, row: PendingAction, workspace: Workspace, *path: PendingStatus
) -> None:
    """Walks a proposal through states, each from the one before."""
    current = PendingStatus.PROPOSED
    for to in path:
        moved = await stores.pending.advance(workspace.id, row.id, expect=current, to=to, now=NOW)
        assert moved is not None
        current = to


LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
FAR_AHEAD = datetime(2100, 1, 1, tzinfo=UTC)  # "everything is older than this"


async def test_a_card_is_found_by_the_message_it_is_on(stores: Stores) -> None:
    row, workspace, _ = await propose(stores)
    await stores.pending.attach_message(workspace.id, row.id, "314")
    chat = row.chat
    found = await stores.pending.find_by_card(workspace.id, chat, "314")
    assert found is not None and found.id == row.id
    assert await stores.pending.find_by_card(workspace.id, chat, "315") is None
    other_chat = chat.model_copy(update={"chat_id": "-999"})
    assert await stores.pending.find_by_card(workspace.id, other_chat, "314") is None
    other, _, _ = await team(stores, user_id="12")
    assert await stores.pending.find_by_card(other.id, chat, "314") is None


async def test_the_sweep_expires_only_waiting_proposals_that_ran_out_of_time(
    stores: Stores,
) -> None:
    late, workspace, _ = await propose(stores, expires_in=timedelta(minutes=5))
    fresh, _, _ = await propose(stores, expires_in=timedelta(hours=5))
    taken, taken_ws, _ = await propose(stores, expires_in=timedelta(minutes=5))
    await move(stores, taken, taken_ws, PendingStatus.CONFIRMED)
    # The database is shared with other tests: look at this test's rows only.
    swept = await stores.pending.expire_due(NOW + timedelta(minutes=10))
    ids = {row.id for row in swept}
    assert late.id in ids and fresh.id not in ids and taken.id not in ids
    assert all(row.status == PendingStatus.EXPIRED for row in swept)
    again = await stores.pending.expire_due(NOW + timedelta(minutes=10))
    assert late.id not in {row.id for row in again}  # a row is swept once
    got = await stores.pending.get(workspace.id, late.id)
    assert got is not None and got.status == PendingStatus.EXPIRED


async def test_the_sweep_fails_runs_that_never_finished(stores: Stores) -> None:
    stuck, ws1, _ = await propose(stores)
    await move(stores, stuck, ws1, PendingStatus.CONFIRMED, PendingStatus.EXECUTING)
    half, ws2, _ = await propose(stores)
    await move(stores, half, ws2, PendingStatus.CONFIRMED)
    waiting, _, _ = await propose(stores)
    done, ws3, _ = await propose(stores)
    await move(
        stores, done, ws3, PendingStatus.CONFIRMED, PendingStatus.EXECUTING, PendingStatus.EXECUTED
    )
    assert await stores.pending.fail_stuck(LONG_AGO) == []  # not old enough yet
    failed = await stores.pending.fail_stuck(FAR_AHEAD)
    ids = {row.id for row in failed}
    assert {stuck.id, half.id} <= ids
    assert waiting.id not in ids and done.id not in ids
    assert all(row.status == PendingStatus.FAILED for row in failed)
    assert all(row.result == {"error": "stuck"} for row in failed)
    again = await stores.pending.fail_stuck(FAR_AHEAD)
    assert not {stuck.id, half.id} & {row.id for row in again}  # a row is failed once


async def test_the_sweep_deletes_only_old_finished_rows(stores: Stores) -> None:
    ended, ws1, _ = await propose(stores)
    await move(stores, ended, ws1, PendingStatus.REJECTED)
    waiting, ws2, _ = await propose(stores)
    running, ws3, _ = await propose(stores)
    await move(stores, running, ws3, PendingStatus.CONFIRMED, PendingStatus.EXECUTING)
    assert await stores.pending.purge(LONG_AGO) == 0  # nothing is old enough
    assert await stores.pending.purge(FAR_AHEAD) >= 1
    assert await stores.pending.get(ws1.id, ended.id) is None
    assert await stores.pending.get(ws2.id, waiting.id) is not None  # still waiting
    assert await stores.pending.get(ws3.id, running.id) is not None  # not finished


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
