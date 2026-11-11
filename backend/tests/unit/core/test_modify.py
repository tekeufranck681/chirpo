"""Changing a proposal by replying to its card (M5.3): who may, what the model is told, what
happens to the old card, and what a reply to a card that is no longer waiting does."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chirpo.core import messages
from chirpo.core.actions import CreateIssue, CreateIssues, IssueDraft, MyIssues, NotARequest
from chirpo.core.domain import ChatUser, RepoPermission
from chirpo.core.errors import MessagingError
from chirpo.core.pending import PendingAction, PendingStatus, Step
from chirpo.core.permissions import Role
from chirpo.core.pipeline import handle_inbound
from tests.fakes import CHAT, make_callback, make_message
from tests.unit.core.test_interpreter import ANNIE, CHECKOUT, FRANCK, SANDBOX, World

ASSIGNED_TO_ANNIE = CreateIssue(
    title="Checkout crashes on Android 13",
    body="Paying by card crashes the app.",
    assignees=("Annie",),
    labels=("bug",),
)


def rows(world: World) -> list[PendingAction]:
    return list(world.services.pending.rows.values())  # type: ignore[attr-defined]


async def ask(world: World, text: str = "log the checkout crash") -> str:
    """Franck's request; returns the id of the card that came back."""
    world.codehost.users |= {"tekeufranck681", "annie-gh"}
    await world.say(text)
    return world.messaging.last_id


async def reply_to(world: World, card: str, text: str, sender: ChatUser = FRANCK) -> str:
    return await world.say(text, sender, reply_to_message_id=card, replies_to_bot=True)


async def test_a_reply_replaces_the_card_with_the_changed_proposal() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    text = await reply_to(world, card, "assign Annie instead, and drop urgent")
    old, new = rows(world)
    assert old.status == PendingStatus.MODIFIED and new.status == PendingStatus.PROPOSED
    assert text.startswith(messages.DRY_RUN_HEADER)
    assert "assigned to annie-gh" in text and "labels: bug" in text
    assert world.messaging.edited[-1][1] == messages.REPLACED  # the old card says so
    assert world.messaging.buttons[card] == ()  # and can't be pressed
    assert world.messaging.buttons[world.messaging.last_id] != ()  # the new one can
    assert world.messaging.sent[-1][2] == "10"  # a reply to the message that asked for the change
    assert world.codehost.issues == {}  # nothing runs until the new card is confirmed


async def test_the_model_is_told_what_is_being_changed() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    await reply_to(world, card, "assign Annie instead")
    asked = world.llm.requests[-1].message
    assert 'kind="pending_proposal"' in asked
    block = asked.split('kind="pending_proposal">')[1].split("</untrusted_")[0]
    assert "create_issue" in block and "Checkout crashes on Android 13" in block
    assert "tekeufranck681" in block  # as stored: people are logins by then


async def test_only_the_new_card_can_be_confirmed() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    old_card = await ask(world)
    await reply_to(world, old_card, "assign Annie instead")
    new_card = world.messaging.last_id
    old, new = rows(world)

    async def press(card: str, row: PendingAction) -> str | None:
        world.messaging.answered.clear()
        callback = make_callback(f"ok:{row.id}", sender=FRANCK, message_id=card)
        await handle_inbound(callback, world.messaging, world.services)
        return world.messaging.answered[0][1]

    assert await press(old_card, old) == messages.CB_ALREADY
    assert world.codehost.issues == {}
    assert await press(new_card, new) == messages.CB_STARTED
    created = world.codehost.issues[(next(iter(world.codehost.issues))[0], 1)]
    assert created.assignees == ("annie-gh",) and created.labels == ("bug",)


async def test_a_member_who_did_not_ask_changes_nothing_by_replying() -> None:
    world = World([CHECKOUT], [MyIssues()])
    await world.linked()
    card = await ask(world)
    await reply_to(world, card, "what's on my plate?", sender=ANNIE)
    assert 'kind="pending_proposal"' not in world.llm.requests[-1].message  # an ordinary request
    [row] = rows(world)
    assert row.status == PendingStatus.PROPOSED
    assert world.messaging.buttons[card] != ()  # the card is untouched


async def test_a_maintainer_can_change_someone_elses_card_and_it_becomes_theirs() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    annie = await world.members.for_chat_user(workspace.id, ANNIE)
    await world.members.set_role(workspace.id, annie.id, Role.MAINTAINER)
    world.codehost.permissions[(SANDBOX.key, "annie-gh")] = RepoPermission.WRITE
    text = await reply_to(world, card, "assign me instead", sender=ANNIE)
    old, new = rows(world)
    assert old.status == PendingStatus.MODIFIED
    assert new.requested_by == annie.id  # it runs with her permissions
    assert messages.CONFIRM_FOOTER.format(name="Annie Ngo") in text


async def test_a_reply_to_a_card_that_already_ran_is_an_ordinary_request() -> None:
    world = World([CHECKOUT], [CreateIssue(title="Second one")])
    await world.linked()
    card = await ask(world)
    [row] = rows(world)
    await handle_inbound(
        make_callback(f"ok:{row.id}", sender=FRANCK, message_id=card),
        world.messaging,
        world.services,
    )
    text = await reply_to(world, card, "thanks, now create another one for the iOS crash")
    assert 'kind="pending_proposal"' not in world.llm.requests[-1].message  # nothing to change
    assert text.startswith(messages.DRY_RUN_HEADER)  # what they asked for is not swallowed
    assert len(rows(world)) == 2


async def test_a_reply_to_a_card_that_ran_out_of_time_is_an_ordinary_request() -> None:
    world = World([CHECKOUT], [CreateIssue(title="Second one")])
    await world.linked()
    card = await ask(world)
    later = datetime.now(UTC) + timedelta(hours=2)
    world.services = replace(world.services, now=lambda: later)
    text = await reply_to(world, card, "also log the iOS crash")
    assert 'kind="pending_proposal"' not in world.llm.requests[-1].message
    assert text.startswith(messages.DRY_RUN_HEADER)


async def test_a_reply_that_only_agrees_points_to_the_buttons() -> None:
    world = World([CHECKOUT], [NotARequest()])
    await world.linked()
    card = await ask(world)
    assert await reply_to(world, card, "yes, looks good") == messages.USE_THE_BUTTONS
    [row] = rows(world)
    assert row.status == PendingStatus.PROPOSED  # chat text never confirms
    assert world.codehost.issues == {}


async def test_a_card_confirmed_while_the_model_was_thinking_is_not_replaced() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    [row] = rows(world)
    real = world.llm.interpret

    async def confirmed_meanwhile(request):  # type: ignore[no-untyped-def]
        await handle_inbound(
            make_callback(f"ok:{row.id}", sender=FRANCK, message_id=card),
            world.messaging,
            world.services,
        )
        return await real(request)

    world.llm.interpret = confirmed_meanwhile  # type: ignore[method-assign]
    text = await reply_to(world, card, "assign Annie instead")
    assert text == messages.CB_ALREADY
    assert len(rows(world)) == 1  # no second card
    assert len(world.codehost.issues) == 1  # what was confirmed ran as it was shown


async def test_a_card_that_expires_while_the_model_thinks_says_expired() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    clock = [datetime.now(UTC)]
    world.services = replace(world.services, now=lambda: clock[0])
    real = world.llm.interpret

    async def time_passes(request):  # type: ignore[no-untyped-def]
        clock[0] += timedelta(hours=2)  # the card runs out of time while the model answers
        return await real(request)

    world.llm.interpret = time_passes  # type: ignore[method-assign]
    assert await reply_to(world, card, "assign Annie instead") == messages.CB_EXPIRED
    assert len(rows(world)) == 1


async def test_if_the_new_card_can_not_be_posted_the_old_one_is_back() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)

    async def telegram_is_down(*_: object, **__: object) -> None:
        raise MessagingError("429 too many requests")

    world.messaging.send_message = telegram_is_down  # type: ignore[method-assign]
    with pytest.raises(MessagingError):
        await reply_to(world, card, "assign Annie instead")
    old, *others = rows(world)
    assert old.status == PendingStatus.PROPOSED  # still there to confirm or cancel
    assert all(row.status == PendingStatus.REJECTED for row in others)  # never reachable
    assert world.messaging.buttons[card] != ()
    assert world.messaging.edited == []  # the old card was never marked replaced


async def test_two_changes_at_the_same_moment_give_one_new_card() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    annie = await world.members.for_chat_user(workspace.id, ANNIE)
    await world.members.set_role(workspace.id, annie.id, Role.MAINTAINER)
    world.codehost.permissions[(SANDBOX.key, "annie-gh")] = RepoPermission.WRITE
    world.llm.delay = 0.01  # both are asking the model when the other arrives
    now = datetime.now(UTC)
    changes = [
        make_message(
            "assign Annie instead",
            sender=who,
            mentions_bot=True,
            reply_to_message_id=card,
            replies_to_bot=True,
            sent_at=now,
            event_id=str(n),
            message_id=str(20 + n),
        )
        for n, who in enumerate((FRANCK, ANNIE))
    ]
    await asyncio.gather(*(handle_inbound(m, world.messaging, world.services) for m in changes))
    old, *new = rows(world)
    assert old.status == PendingStatus.MODIFIED
    assert [row.status for row in new] == [PendingStatus.PROPOSED]  # one new card, not two


async def test_a_card_too_big_to_show_the_model_whole_is_not_changed_by_reply() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await ask(world)
    [small] = rows(world)
    drafts = tuple(IssueDraft(title=f"Issue {n}", body="b" * 9000) for n in range(10))
    big = Step.of(CreateIssues(repo=SANDBOX.full_name, issues=drafts), SANDBOX)
    row = await world.services.pending.create(
        workspace_id=small.workspace_id,
        requested_by=small.requested_by,
        chat=CHAT,
        steps=[big],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await world.services.pending.attach_message(row.workspace_id, row.id, "555")
    asked_before = len(world.llm.requests)
    text = await reply_to(world, "555", "drop the last one")
    assert text == messages.TOO_BIG_TO_CHANGE
    assert len(world.llm.requests) == asked_before  # the model never sees a cut-off proposal
    assert card != "555"


async def test_a_reply_that_asks_a_question_leaves_the_card_waiting() -> None:
    world = World([CHECKOUT], [MyIssues()])
    await world.linked()
    card = await ask(world)
    await reply_to(world, card, "what's on my plate?")
    [row] = rows(world)
    assert row.status == PendingStatus.PROPOSED
    assert world.messaging.buttons[card] != ()


async def test_a_change_is_audited_with_both_cards_and_the_original_asker() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    await reply_to(world, card, "assign Annie instead")
    old, new = rows(world)
    [entry] = world.audit.entries
    assert entry.action == "modify_proposal"
    assert entry.actor_member_id == new.requested_by
    assert entry.payload == {
        "replaced": str(old.id),
        "by": str(new.id),
        "original_asker": str(old.requested_by),
    }


async def test_the_new_card_says_it_replaces_the_old_one() -> None:
    world = World([CHECKOUT], [ASSIGNED_TO_ANNIE])
    await world.linked()
    card = await ask(world)
    text = await reply_to(world, card, "assign Annie instead")
    assert messages.REPLACES_NOTE in text


async def test_an_answer_that_loses_the_new_issue_leaves_the_card_as_it_was() -> None:
    """The model sometimes reads "assign it to me" as an action on an existing issue. A card
    that creates an issue must not be replaced by an answer that creates none."""
    from chirpo.core.actions import AssignIssue, IssueRef
    from chirpo.core.domain import NewIssue

    world = World([CHECKOUT], [AssignIssue(issue=IssueRef(number=1), assignees=("me",))])
    await world.linked()
    world.codehost.users |= {"tekeufranck681"}
    await world.codehost.create_issue(SANDBOX, NewIssue(title="Login button broken on Safari"))
    card = await ask(world)
    text = await reply_to(world, card, "assign it to me")
    assert text == messages.CARD_UNCHANGED
    [row] = rows(world)
    assert row.status == PendingStatus.PROPOSED  # the card is still there to confirm
    assert world.messaging.buttons[card] != ()
    assert world.messaging.edited == []


async def test_a_card_that_creates_nothing_can_still_be_swapped_for_another_action() -> None:
    from chirpo.core.actions import CloseIssue, IssueRef, ReopenIssue
    from chirpo.core.domain import NewIssue

    world = World([CloseIssue(issue=IssueRef(number=1))], [ReopenIssue(issue=IssueRef(number=1))])
    await world.linked()
    world.codehost.users |= {"tekeufranck681"}
    await world.codehost.create_issue(SANDBOX, NewIssue(title="Login button broken on Safari"))
    card = await ask(world, "close #1")
    text = await reply_to(world, card, "no, reopen it instead")
    assert text.startswith(messages.DRY_RUN_HEADER)
    old, new = rows(world)
    assert (old.status, new.status) == (PendingStatus.MODIFIED, PendingStatus.PROPOSED)
