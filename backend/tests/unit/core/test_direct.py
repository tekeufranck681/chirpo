"""Status and label changes that run without a card (M5.6): only on an issue the sender named,
with an Undo that reverses exactly what they did. Everything else keeps asking first."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chirpo.core import messages
from chirpo.core.actions import (
    AddLabels,
    AssignIssue,
    CreateIssue,
    IssueRef,
    RemoveLabels,
    SetStatus,
)
from chirpo.core.direct import named_in, runs_without_card
from chirpo.core.domain import AuditResult, Button, ChatUser, RepoPermission, RepoRef
from chirpo.core.errors import CodeHostForbiddenError
from chirpo.core.pending import PendingAction, PendingStatus
from chirpo.core.permissions import PermissionChecker, Role
from chirpo.core.pipeline import handle_inbound
from chirpo.core.resolution import Resolved
from tests.fakes import CHAT, make_callback, make_message
from tests.unit.core.test_interpreter import ANNIE, FRANCK, SANDBOX, World, _issues

REPO = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")

# ------------------------------------------------------------------ is the issue named?


@pytest.mark.parametrize(
    "text",
    ["I'm starting #12", "start issue 12 now", "ticket #12 is done", "Ticket 12 again", "see #12."],
)
def test_a_number_the_sender_wrote_names_the_issue(text: str) -> None:
    assert named_in(IssueRef(number=12), text)


@pytest.mark.parametrize(
    "text",
    [
        "I have 12 apples",
        "#123 is done",
        "#1 and #2",
        "version 12.3 shipped",
        "",
        "it crashed on bug 12 times",  # "bug" is not a way of naming an issue
        "task 12 of the sprint",
        "story 12 points",
        "#12th floor",
        "#12.5 percent",
    ],
)
def test_a_number_that_is_only_part_of_something_else_does_not(text: str) -> None:
    assert not named_in(IssueRef(number=12), text)


def test_the_replied_to_message_can_name_the_issue_when_it_names_only_that_one() -> None:
    assert named_in(IssueRef(number=12), "move it to review", "see #12 for the details")
    assert named_in(IssueRef(number=12), "move it", "#12 is blocked, and #12 again")


def test_a_replied_to_message_that_names_several_issues_does_not_settle_which() -> None:
    assert not named_in(IssueRef(number=13), "mark it in review", "open: #12 and #13")
    assert not named_in(IssueRef(number=12), "mark it in review", "see #12, also issue 14")


def test_the_senders_own_number_wins_even_if_the_replied_to_message_lists_many() -> None:
    assert named_in(IssueRef(number=13), "mark #13 in review", "open: #12 and #13 and #14")


def test_a_description_must_come_from_the_senders_own_words() -> None:
    ref = IssueRef(description="the checkout bug")
    assert named_in(ref, "I'm starting on the checkout bug")
    assert named_in(ref, "CHECKOUT is done")
    assert not named_in(ref, "move it to review", "the checkout bug is still there")  # not theirs
    assert not named_in(ref, "move the crash to review")
    two_words = IssueRef(description="avatar upload crash")
    assert not named_in(two_words, "the avatar thing is done")  # one word of two is not enough


def test_a_description_of_only_filler_words_names_nothing() -> None:
    assert not named_in(IssueRef(description="the one"), "the one")


# ------------------------------------------------------- which requests skip the card


def step(proposal: object, ref: IssueRef | None) -> Resolved:
    return Resolved(proposal=proposal, repo=REPO, ref=ref)  # type: ignore[arg-type]


def status(number: int = 12) -> SetStatus:
    return SetStatus(repo=REPO.full_name, issue=IssueRef(number=number), status="status: review")


def test_named_status_and_label_changes_run_without_a_card() -> None:
    steps = [
        step(status(), IssueRef(number=12)),
        step(
            AddLabels(repo=REPO.full_name, issue=IssueRef(number=12), labels=("bug",)),
            IssueRef(number=12),
        ),
        step(
            RemoveLabels(repo=REPO.full_name, issue=IssueRef(number=12), labels=("bug",)),
            IssueRef(number=12),
        ),
    ]
    assert runs_without_card(steps, "move #12 to review, add bug, remove bug")


def test_one_step_that_asks_first_puts_the_whole_request_on_a_card() -> None:
    assign = AssignIssue(repo=REPO.full_name, issue=IssueRef(number=12), assignees=("annie-gh",))
    create = CreateIssue(repo=REPO.full_name, title="Export")
    text = "#12 to review, give it to Annie, and log the export"
    assert not runs_without_card([step(status(), IssueRef(number=12)), step(assign, None)], text)
    assert not runs_without_card([step(status(), IssueRef(number=12)), step(create, None)], text)


def test_an_unnamed_issue_means_a_card() -> None:
    assert not runs_without_card([step(status(), IssueRef(number=12))], "move it to review")
    assert not runs_without_card([step(status(), None)], "move #12 to review")


def test_nothing_to_run_is_not_a_run() -> None:
    assert not runs_without_card([], "#12")


# -------------------------------------------------------------------------- end to end


def rows(world: World) -> list[PendingAction]:
    return list(world.services.pending.rows.values())  # type: ignore[attr-defined]


async def ready(world: World) -> None:
    await world.linked()
    await _issues(world)  # #1 checkout crash, #2 dark mode, #3 CSV export


def issue_labels(world: World, number: int) -> tuple[str, ...]:
    return world.codehost.issues[(SANDBOX.key, number)].labels


async def test_naming_the_issue_runs_a_status_change_at_once_with_an_undo() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="in progress")])
    await ready(world)
    text = await world.say("I'm starting #2")
    assert text == messages.DONE_STATUS.format(issue='#2 "Add dark mode"', status="in progress")
    assert issue_labels(world, 2) == ("status: in progress",)
    [row] = rows(world)
    assert row.status == PendingStatus.EXECUTED
    assert row.result["effects"][0]["added"] == ["status: in progress"]
    assert world.messaging.buttons[world.messaging.last_id] == (
        Button(text=messages.BUTTON_UNDO, data=f"undo:{row.id}"),
    )
    assert world.messaging.sent[-1][2] == "10"  # a reply to the message that asked
    assert row.message_id == world.messaging.last_id  # the Undo button's own message
    entry = world.audit.entries[-1]
    assert (entry.action, entry.result) == ("set_status", AuditResult.EXECUTED)
    assert entry.confirmed_by_member_id is None  # nobody had to confirm it


async def test_labels_run_at_once_too() -> None:
    world = World([AddLabels(issue=IssueRef(number=2), labels=("enhancement",))])
    await ready(world)
    await world.say("tag #2 as an enhancement")
    assert issue_labels(world, 2) == ("enhancement",)


async def test_an_issue_found_only_from_context_still_asks_first() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    text = await world.say("move it to review")  # no number in the message
    assert text.startswith(messages.DRY_RUN_HEADER)
    assert issue_labels(world, 2) == ()  # nothing changed yet
    assert world.messaging.buttons[world.messaging.last_id][0].text == messages.BUTTON_CONFIRM


async def test_an_issue_named_in_the_replied_to_message_runs_at_once() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    earlier = make_message(
        "dark mode is #2, nobody has it yet",
        sender=ANNIE,
        message_id="50",
        sent_at=datetime.now(UTC),
    )
    await world.buffer.remember(workspace.id, earlier)
    text = await world.say("move it to review", reply_to_message_id="50")
    assert text.startswith('#2 "Add dark mode" is now')
    assert issue_labels(world, 2) == ("status: review",)


async def test_a_replied_to_message_that_lists_several_issues_asks_first() -> None:
    world = World([SetStatus(issue=IssueRef(number=3), status="review")])
    await ready(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    listing = make_message(
        "open: #2 dark mode, #3 CSV export",
        sender=ANNIE,
        message_id="50",
        sent_at=datetime.now(UTC),
    )
    await world.buffer.remember(workspace.id, listing)
    text = await world.say("mark it in review", reply_to_message_id="50")
    assert text.startswith(messages.DRY_RUN_HEADER)  # "it" could be either: a card first
    assert issue_labels(world, 3) == ()


async def test_an_issue_named_by_its_own_words_runs_at_once() -> None:
    world = World([SetStatus(issue=IssueRef(description="the checkout bug"), status="review")])
    await ready(world)
    text = await world.say("I'm done with the checkout bug, it's in review")
    assert text.startswith('#1 "Checkout crashes on Android 13" is now')


async def test_a_description_in_none_of_the_sender_s_words_asks_first() -> None:
    world = World([SetStatus(issue=IssueRef(description="the checkout bug"), status="review")])
    await ready(world)
    text = await world.say("move the crash we saw earlier to review")  # the model guessed
    assert text.startswith(messages.DRY_RUN_HEADER)
    assert "status: review" not in issue_labels(world, 1)


async def test_a_message_that_waited_in_the_queue_asks_first() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    old = datetime.now(UTC) - timedelta(minutes=10)
    text = await world.say("I'm starting #2", sent_at=old)
    assert text.startswith(messages.DRY_RUN_HEADER)
    assert messages.STALE_NOTE in text
    assert issue_labels(world, 2) == ()


async def test_a_mixed_request_is_one_card_for_everything() -> None:
    world = World(
        [SetStatus(issue=IssueRef(number=2), status="review"), CreateIssue(title="Export to CSV")]
    )
    await ready(world)
    await world.say("move #2 to review and log the CSV export")
    assert len(world.messaging.sent) == 1  # one message, one decision
    card = world.messaging.sent[0][1]
    assert card.startswith(messages.DRY_RUN_HEADER)
    assert "Move" in card and "Create an issue" in card
    assert issue_labels(world, 2) == ()


async def test_a_change_that_failed_has_no_undo() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)

    async def refuse(*_: object, **__: object) -> None:
        raise CodeHostForbiddenError("no")

    world.codehost.update_issue = refuse  # type: ignore[method-assign]
    text = await world.say("move #2 to review")
    reason = messages.STEP_REFUSED.format(reason="CodeHostForbiddenError")
    assert messages.STOPPED.format(reason=reason) in text
    assert world.messaging.buttons[world.messaging.last_id] == ()
    assert rows(world)[0].status == PendingStatus.FAILED


# ------------------------------------------------------------------------------- Undo


async def press_undo(world: World, *, sender: ChatUser = FRANCK) -> str | None:
    [row, *_] = rows(world)
    world.messaging.answered.clear()
    callback = make_callback(f"undo:{row.id}", sender=sender, message_id=row.message_id or "")
    await handle_inbound(callback, world.messaging, world.services)
    assert len(world.messaging.answered) == 1
    return world.messaging.answered[0][1]


async def change_and_press_undo(
    world: World, *, sender: ChatUser = FRANCK, message: str = "I'm starting #2"
) -> str | None:
    await world.say(message)
    return await press_undo(world, sender=sender)


async def test_undo_reverses_the_change_and_says_so() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="in progress")])
    await ready(world)
    note = await change_and_press_undo(world)
    assert note == messages.CB_STARTED
    assert issue_labels(world, 2) == ()
    shown = world.messaging.edited[-1][1]
    assert shown.startswith(messages.UNDONE_HEADER)
    assert messages.DONE_UNDO_LABELS.format(issue='#2 "Add dark mode"') in shown
    assert world.messaging.buttons[rows(world)[0].message_id or ""] == ()  # nothing to press
    assert rows(world)[0].status == PendingStatus.UNDONE
    entry = world.audit.entries[-1]
    assert (entry.action, entry.result) == ("undo_labels", AuditResult.EXECUTED)


async def test_undoing_twice_changes_nothing_the_second_time() -> None:
    world = World([AddLabels(issue=IssueRef(number=2), labels=("enhancement",))])
    await ready(world)
    await change_and_press_undo(world, message="tag #2 as an enhancement")
    current = world.codehost.issues[(SANDBOX.key, 2)]
    world.codehost.issues[(SANDBOX.key, 2)] = current.model_copy(
        update={"labels": ("enhancement",)}  # somebody put it back by hand
    )
    assert await press_undo(world) == messages.CB_UNDONE
    assert issue_labels(world, 2) == ("enhancement",)  # the second press did not touch it


async def test_two_presses_at_once_undo_once() -> None:
    world = World([AddLabels(issue=IssueRef(number=2), labels=("enhancement",))])
    await ready(world)
    await world.say("tag #2 as an enhancement")
    [row] = rows(world)
    callbacks = [
        make_callback(f"undo:{row.id}", sender=FRANCK, message_id=row.message_id or "", event_id=n)
        for n in ("1", "2")
    ]
    await asyncio.gather(*(handle_inbound(c, world.messaging, world.services) for c in callbacks))
    notes = sorted(note or "" for _, note in world.messaging.answered)
    assert notes == sorted([messages.CB_STARTED, messages.CB_UNDONE])


async def test_someone_who_did_not_ask_can_not_undo() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    note = await change_and_press_undo(world, sender=ANNIE)
    assert note == messages.CB_NOT_ALLOWED.format(name="Tekeu Franck")
    assert issue_labels(world, 2) == ("status: review",)
    assert rows(world)[0].status == PendingStatus.EXECUTED


async def test_a_maintainer_can_undo_and_is_recorded_as_the_one_who_did() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    annie = await world.members.for_chat_user(workspace.id, ANNIE)
    await world.members.set_role(workspace.id, annie.id, Role.MAINTAINER)
    assert await change_and_press_undo(world, sender=ANNIE) == messages.CB_STARTED
    assert issue_labels(world, 2) == ()
    assert world.audit.entries[-1].confirmed_by_member_id == annie.id


async def test_after_an_hour_it_is_too_late() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    await world.say("move #2 to review")
    later = datetime.now(UTC) + timedelta(hours=2)
    world.services = replace(world.services, now=lambda: later)
    assert await press_undo(world) == messages.CB_UNDO_LATE
    assert issue_labels(world, 2) == ("status: review",)
    assert rows(world)[0].status == PendingStatus.EXECUTED


async def test_if_the_asker_lost_access_the_undo_is_refused_and_can_be_retried() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    await world.say("move #2 to review")
    world.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.READ
    world.services = replace(world.services, permissions=PermissionChecker(world.codehost))
    await press_undo(world)
    refusal = messages.STOPPED.format(
        reason=messages.STEP_NO_LONGER_ALLOWED.format(name="Tekeu Franck")
    )
    assert world.messaging.sent[-1][1] == refusal
    assert rows(world)[0].status == PendingStatus.EXECUTED  # still undoable
    assert issue_labels(world, 2) == ("status: review",)
    world.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.WRITE
    world.services = replace(world.services, permissions=PermissionChecker(world.codehost))
    assert await press_undo(world) == messages.CB_STARTED
    assert issue_labels(world, 2) == ()


async def test_a_press_on_another_message_does_not_undo() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    await world.say("move #2 to review")
    [row] = rows(world)
    world.messaging.answered.clear()
    callback = make_callback(f"undo:{row.id}", sender=FRANCK, message_id="999")
    await handle_inbound(callback, world.messaging, world.services)
    assert world.messaging.answered[0][1] == messages.CB_UNAVAILABLE
    assert issue_labels(world, 2) == ("status: review",)


# --------------------------------------------------------- a run that hits trouble


async def test_a_run_that_can_not_start_changes_nothing_and_says_so() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    real = world.services.pending.advance

    async def lost(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("to") == PendingStatus.CONFIRMED:
            return None  # e.g. the row was swept in the meantime
        return await real(*args, **kwargs)

    world.services.pending.advance = lost  # type: ignore[method-assign]
    text = await world.say("move #2 to review")
    assert text == messages.NOT_RUN
    assert issue_labels(world, 2) == ()


async def test_if_the_result_can_not_be_stored_the_person_is_still_told_without_an_undo() -> None:
    world = World([SetStatus(issue=IssueRef(number=2), status="review")])
    await ready(world)
    real = world.services.pending.advance

    async def database_blip(*args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("to") == PendingStatus.EXECUTED:
            raise RuntimeError("connection reset")
        return await real(*args, **kwargs)

    world.services.pending.advance = database_blip  # type: ignore[method-assign]
    text = await world.say("move #2 to review")
    assert text.startswith('#2 "Add dark mode" is now')  # they know it happened
    assert world.messaging.buttons[world.messaging.last_id] == ()  # nothing to undo from
    assert issue_labels(world, 2) == ("status: review",)
