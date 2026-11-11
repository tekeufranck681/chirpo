"""From a request to a card to a press: who may confirm, what a press does, how it fails (M5.2,
M5.5). Uses the interpreter tests' World: fake chat, fake GitHub, stored cards."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from chirpo.core import messages
from chirpo.core.actions import (
    CloseIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    IssueRef,
    MyIssues,
)
from chirpo.core.domain import (
    AuditResult,
    ChatRef,
    ChatUser,
    InboundCallback,
    NewIssue,
)
from chirpo.core.errors import MessagingError
from chirpo.core.pending import PendingAction, PendingStatus
from chirpo.core.permissions import PermissionChecker, Role
from chirpo.core.pipeline import handle_inbound
from tests.fakes import CHAT, make_callback
from tests.unit.core.test_interpreter import (
    ANNIE,
    CHECKOUT,
    FRANCK,
    SANDBOX,
    World,
)

ZWSP = "​"


async def propose(world: World, *, text: str = "log the checkout crash") -> str:
    """Sends a request and returns the id of the card the bot posted."""
    world.codehost.users |= {"tekeufranck681", "annie-gh"}
    await world.say(text)
    return str(100 + len(world.messaging.sent))


def pending(world: World) -> PendingAction:
    [row] = world.services.pending.rows.values()  # type: ignore[attr-defined]
    return row


async def press(
    world: World,
    card: str,
    which: str = "ok",
    *,
    sender: ChatUser = FRANCK,
    chat: ChatRef = CHAT,
    data: str | None = None,
) -> str | None:
    """Presses a button; returns the note only the presser sees."""
    world.messaging.answered.clear()
    callback = make_callback(
        data if data is not None else f"{which}:{pending(world).id}",
        sender=sender,
        message_id=card,
        chat=chat,
    )
    await handle_inbound(callback, world.messaging, world.services)
    assert len(world.messaging.answered) == 1  # every press is acknowledged, or the app spins
    return world.messaging.answered[0][1]


async def test_a_request_becomes_a_card_with_two_buttons_and_nothing_runs() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    text = world.messaging.sent[-1][1]
    assert text.startswith(messages.DRY_RUN_HEADER)
    assert text.endswith(messages.CONFIRM_FOOTER.format(name="Tekeu Franck"))
    confirm, cancel = world.messaging.buttons[card]
    row = pending(world)
    assert (confirm.text, confirm.data) == (messages.BUTTON_CONFIRM, f"ok:{row.id}")
    assert (cancel.text, cancel.data) == (messages.BUTTON_CANCEL, f"no:{row.id}")
    assert (row.status, row.message_id, row.chat) == (PendingStatus.PROPOSED, card, CHAT)
    assert row.steps[0].action == "create_issue"
    assert row.expires_at > datetime.now(UTC) + timedelta(minutes=59)
    assert world.codehost.issues == {}


async def test_the_person_who_asked_confirms_and_the_issue_is_created() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    assert await press(world, card) == messages.CB_STARTED
    created = world.codehost.issues[(SANDBOX.key, 1)]
    assert created.title == "Checkout crashes on Android 13"
    assert created.assignees == ("tekeufranck681",)
    assert created.labels == ("bug",)
    shown, final = world.messaging.edited[-2][1], world.messaging.edited[-1][1]
    assert shown == messages.RUNNING
    assert final == messages.ISSUE_CREATED.format(number=1, title=created.title, url=created.url)
    assert world.messaging.buttons[card] == ()  # nothing left to press
    assert pending(world).status == PendingStatus.EXECUTED
    [entry] = world.audit.entries
    assert entry.result == AuditResult.EXECUTED
    assert entry.actor_member_id == entry.confirmed_by_member_id  # asked and confirmed by Franck


async def test_pressing_twice_creates_one_issue() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    await press(world, card)
    assert await press(world, card) == messages.CB_ALREADY
    assert len(world.codehost.issues) == 1


async def test_two_presses_at_the_same_moment_create_one_issue() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    data = f"ok:{pending(world).id}"
    callbacks = [
        make_callback(data, sender=FRANCK, message_id=card, event_id=str(n)) for n in (1, 2)
    ]
    await asyncio.gather(*(handle_inbound(c, world.messaging, world.services) for c in callbacks))
    assert len(world.codehost.issues) == 1
    notes = sorted(note or "" for _, note in world.messaging.answered)
    assert notes == sorted([messages.CB_STARTED, messages.CB_ALREADY])


async def test_a_member_who_did_not_ask_can_not_confirm() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    note = await press(world, card, sender=ANNIE)
    assert note == messages.CB_NOT_ALLOWED.format(name="Tekeu Franck")
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.PROPOSED
    assert world.messaging.edited == []  # the card still waits for the right person


async def test_a_maintainer_can_confirm_someone_elses_request() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    annie = await world.members.for_chat_user(workspace.id, ANNIE)
    await world.members.set_role(workspace.id, annie.id, Role.MAINTAINER)
    assert await press(world, card, sender=ANNIE) == messages.CB_STARTED
    assert len(world.codehost.issues) == 1
    [entry] = world.audit.entries
    assert entry.actor_member_id != entry.confirmed_by_member_id
    assert entry.confirmed_by_member_id == annie.id


async def test_an_anonymous_sender_can_never_confirm() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    group = ChatUser(platform="fake", user_id="chat:-100", display_name="Group", is_anonymous=True)
    assert await press(world, card, sender=group) == messages.CB_ANONYMOUS
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.PROPOSED


async def test_the_asker_cancels() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    assert await press(world, card, "no") == messages.CB_CANCELLED
    assert world.messaging.edited[-1][1] == messages.CANCELLED.format(name="Tekeu Franck")
    assert world.messaging.buttons[card] == ()
    assert pending(world).status == PendingStatus.REJECTED
    assert [e.result for e in world.audit.entries] == [AuditResult.REJECTED]
    assert await press(world, card) == messages.CB_ALREADY  # a cancelled card can't be confirmed
    assert world.codehost.issues == {}


async def test_someone_else_can_not_cancel_it_either() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    assert await press(world, card, "no", sender=ANNIE) == messages.CB_NOT_ALLOWED.format(
        name="Tekeu Franck"
    )
    assert pending(world).status == PendingStatus.PROPOSED


async def test_a_press_must_come_from_the_card_it_belongs_to() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    assert await press(world, "999") == messages.CB_UNAVAILABLE  # another message
    other = ChatRef(platform="fake", chat_id="-200")
    assert await press(world, card, chat=other) == messages.CB_UNAVAILABLE  # another chat
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.PROPOSED


@pytest.mark.parametrize(
    "data",
    ["ok", "ok:", "ok:not-a-uuid", "no:1234", f"zz:{uuid4()}", f"ok:{uuid4()}", "", "x" * 60],
)
async def test_made_up_button_data_does_nothing(data: str) -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    note = await press(world, card, data=data)
    assert note in (None, messages.CB_UNAVAILABLE)
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.PROPOSED


async def test_a_card_of_another_team_can_not_be_pressed_from_this_chat() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    other_chat = ChatRef(platform="fake", chat_id="-200")
    other = await world.workspaces.get_or_create_for_chat(other_chat, name="Other")
    await world.workspaces.link_repo(other.id, SANDBOX)
    assert await press(world, card, sender=FRANCK, chat=other_chat) == messages.CB_UNAVAILABLE
    assert world.codehost.issues == {}


async def test_an_old_card_expires_instead_of_running() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    later = datetime.now(UTC) + timedelta(hours=2)
    world.services = replace(world.services, now=lambda: later)
    assert await press(world, card) == messages.CB_EXPIRED
    assert world.messaging.edited[-1][1] == messages.EXPIRED
    assert pending(world).status == PendingStatus.EXPIRED
    assert world.codehost.issues == {}


async def test_the_asker_lost_access_before_confirming() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    world.codehost.permissions.clear()  # no access to the repo any more...
    world.services = replace(world.services, permissions=PermissionChecker(world.codehost))
    # ...seen at once: a fresh checker, since granted access is cached for a minute (D39)
    await press(world, card)
    expected = messages.STOPPED.format(
        reason=messages.STEP_NO_LONGER_ALLOWED.format(name="Tekeu Franck")
    )
    assert world.messaging.edited[-1][1] == expected
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.FAILED


async def test_an_issue_that_vanished_is_reported_not_guessed() -> None:
    world = World([CloseIssue(issue=IssueRef(number=1))])
    await world.linked()
    world.codehost.users |= {"tekeufranck681"}
    await world.codehost.create_issue(SANDBOX, NewIssue(title="Checkout crashes"))
    card = await propose(world, text="close #1")
    del world.codehost.issues[(SANDBOX.key, 1)]
    await press(world, card)
    assert world.messaging.edited[-1][1] == messages.STOPPED.format(reason=messages.STEP_NOT_FOUND)
    assert pending(world).status == PendingStatus.FAILED
    assert [e.result for e in world.audit.entries] == [AuditResult.FAILED]


async def test_a_crash_in_a_later_step_keeps_what_already_ran() -> None:
    world = World([CHECKOUT, CreateIssue(title="Second")])
    await world.linked()
    card = await propose(world)
    real = world.codehost.create_issue
    calls = 0

    async def second_one_breaks(repo: object, issue: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom")
        return await real(repo, issue)  # type: ignore[arg-type]

    world.codehost.create_issue = second_one_breaks  # type: ignore[method-assign]
    await press(world, card)
    text = world.messaging.edited[-1][1]
    assert text.startswith("Created #1")  # people see the first issue exists: no duplicate retry
    assert messages.STOPPED.format(reason=messages.STEP_CRASHED) in text
    assert pending(world).status == PendingStatus.FAILED
    assert [e.result for e in world.audit.entries] == [AuditResult.EXECUTED, AuditResult.FAILED]


async def test_work_that_dies_after_confirming_never_leaves_the_card_running() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)

    async def cancelled(*_: object, **__: object) -> None:
        raise asyncio.CancelledError

    world.codehost.create_issue = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await press(world, card)
    assert pending(world).status == PendingStatus.FAILED  # not stuck in executing
    assert world.messaging.edited[-1][1] == messages.CRASHED


async def test_a_card_that_expires_during_the_press_says_expired_not_already_handled() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    row = pending(world)
    clock = iter([row.expires_at - timedelta(seconds=1), row.expires_at + timedelta(seconds=1)])
    world.services = replace(world.services, now=lambda: next(clock, row.expires_at))
    assert await press(world, card) == messages.CB_EXPIRED
    assert pending(world).status == PendingStatus.EXPIRED
    assert world.codehost.issues == {}


async def test_long_bodies_are_shortened_so_every_step_still_fits_on_the_card() -> None:
    drafts = tuple(IssueDraft(title=f"Issue {n}", body="b" * 900) for n in range(10))
    world = World([CreateIssues(issues=drafts)])
    await world.linked()
    card = await propose(world)
    text = world.messaging.sent[-1][1]
    assert len(text) <= 4096
    assert all(f"Issue {n}" in text for n in range(10))  # nothing is cut away
    await press(world, card)
    assert len(world.messaging.edited[-1][1]) <= 4096


async def test_a_request_too_big_to_show_is_not_offered_for_confirming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("chirpo.core.confirmation.MAX_CARD", 300)
    drafts = tuple(IssueDraft(title="T" * 100) for _ in range(5))
    world = World([CreateIssues(issues=drafts)])
    await world.linked()
    await world.say("log all of these")
    assert world.messaging.sent[-1][1] == messages.TOO_BIG
    assert world.services.pending.rows == {}  # type: ignore[attr-defined]
    assert world.codehost.issues == {}


async def test_the_result_is_sent_again_when_the_card_can_not_be_edited() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)

    async def refuse(*_: object, **__: object) -> None:
        raise MessagingError("message to edit not found")

    world.messaging.edit_message = refuse  # type: ignore[method-assign]
    await press(world, card)
    assert len(world.codehost.issues) == 1
    assert world.messaging.sent[-1][1].startswith("Created #1")
    assert world.messaging.sent[-1][2] == card  # a reply to the card


async def test_the_card_shows_the_text_without_a_live_mention_and_the_post_is_cleaned() -> None:
    risky = CreateIssue(title="Crash", body="Ask @annie_n, fixes #3. [docs](https://evil.example)")
    world = World([risky])
    await world.linked()
    card = await propose(world)
    card_text = world.messaging.sent[-1][1]
    assert "@annie_n" not in card_text and f"@{ZWSP}annie_n" in card_text  # Telegram would ping
    assert "[docs](https://evil.example)" in card_text  # the link is shown as written
    await press(world, card)
    body = world.codehost.issues[(SANDBOX.key, 1)].body
    assert f"@{ZWSP}annie_n" in body and f"fixes{ZWSP} #3" in body
    assert "[docs]" not in body and "\\[docs\\](https://evil.example)" in body


async def test_a_press_from_a_bot_is_ignored() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    robot = FRANCK.model_copy(update={"is_bot": True})
    callback: InboundCallback = make_callback(
        f"ok:{pending(world).id}", sender=robot, message_id=card
    )
    await handle_inbound(callback, world.messaging, world.services)
    assert world.codehost.issues == {}
    assert pending(world).status == PendingStatus.PROPOSED


async def test_reads_are_answered_and_the_change_gets_its_own_card() -> None:
    world = World([MyIssues(), CHECKOUT])
    await world.linked()
    world.codehost.users |= {"tekeufranck681"}
    await world.say("what's mine, and log the checkout crash")
    first, second = world.messaging.sent
    assert messages.ISSUES_TITLE.split(" {")[0] in first[1] or "No " in first[1]
    assert second[1].startswith(messages.DRY_RUN_HEADER)
    assert world.messaging.buttons["102"] != ()


async def test_a_long_body_is_cut_on_the_card_with_a_count_but_stored_whole() -> None:
    long_body = "x" * 700
    world = World([CreateIssue(title="Crash", body=long_body)])
    await world.linked()
    await propose(world)
    card_text = world.messaging.sent[-1][1]
    assert "x" * 600 + messages.DRY_MORE.format(count=100) in card_text
    assert "x" * 601 not in card_text
    assert pending(world).steps[0].proposal["body"] == long_body  # Confirm runs the whole text


async def test_a_second_press_while_it_is_running_says_so() -> None:
    world = World([CHECKOUT])
    await world.linked()
    card = await propose(world)
    started, release = asyncio.Event(), asyncio.Event()
    real = world.codehost.create_issue

    async def slow(repo: object, issue: object) -> object:
        started.set()
        await release.wait()
        return await real(repo, issue)  # type: ignore[arg-type]

    world.codehost.create_issue = slow  # type: ignore[method-assign]
    first = asyncio.create_task(press(world, card))
    await started.wait()  # the first press is on GitHub now
    world.messaging.answered.clear()
    second = make_callback(f"ok:{pending(world).id}", sender=FRANCK, message_id=card, event_id="9")
    await handle_inbound(second, world.messaging, world.services)
    assert [note for _, note in world.messaging.answered] == [messages.CB_RUNNING]
    release.set()
    await first
    assert len(world.codehost.issues) == 1
    assert await press(world, card) == messages.CB_ALREADY  # once it is over: handled
