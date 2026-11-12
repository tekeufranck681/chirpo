"""M5 acceptance, as one story on the fake adapters: a request becomes a card, the card is changed
by replying, Confirm is double-tapped, and exactly one issue is created and audited. Each step
has its own focused tests (test_modify, test_confirmation); this one checks they work together."""

import asyncio

from chirpo.core import messages
from chirpo.core.actions import CreateIssue
from chirpo.core.domain import AuditResult
from chirpo.core.pending import PendingStatus
from chirpo.core.pipeline import handle_inbound
from tests.fakes import make_callback
from tests.unit.core.test_interpreter import FRANCK, SANDBOX, World

REQUEST = "checkout crashes on Android 13, give it to Annie, it's urgent"

FIRST_PROPOSAL = CreateIssue(
    title="Checkout crashes on Android 13",
    body="Paying by card crashes the app.",
    assignees=("Annie",),
    labels=("bug", "urgent"),
)
CHANGED_PROPOSAL = CreateIssue(
    title="Checkout crashes on Android 13",
    body="Paying by card crashes the app.",
    assignees=("Franck",),
    labels=("bug", "urgent"),
)


async def test_request_modify_double_confirm_creates_one_audited_issue() -> None:
    world = World([FIRST_PROPOSAL], [CHANGED_PROPOSAL])
    await world.linked()
    world.codehost.users |= {"tekeufranck681", "annie-gh"}

    # 1. The request becomes a card; nothing exists on GitHub yet.
    card_text = await world.say(REQUEST)
    first_card = world.messaging.last_id
    assert card_text.startswith(messages.DRY_RUN_HEADER)
    assert '"Checkout crashes on Android 13"' in card_text and "assigned to annie-gh" in card_text
    assert world.codehost.issues == {}

    # 2. Franck changes his mind by replying to the card: a new card replaces the old one.
    changed_text = await world.say(
        "give it to me instead", FRANCK, reply_to_message_id=first_card, replies_to_bot=True
    )
    second_card = world.messaging.last_id
    assert "assigned to tekeufranck681" in changed_text
    assert world.messaging.edited[-1][1] == messages.REPLACED
    assert world.messaging.buttons[first_card] == ()
    old, new = world.services.pending.rows.values()  # type: ignore[attr-defined]
    assert (old.status, new.status) == (PendingStatus.MODIFIED, PendingStatus.PROPOSED)
    assert world.codehost.issues == {}

    # 3. The old card can no longer create anything.
    world.messaging.answered.clear()
    stale = make_callback(f"ok:{old.id}", sender=FRANCK, message_id=first_card, event_id="9")
    await handle_inbound(stale, world.messaging, world.services)
    assert world.messaging.answered[0][1] == messages.CB_ALREADY
    assert world.codehost.issues == {}

    # 4. Confirm is tapped twice at the same moment on the new card: one issue.
    world.messaging.answered.clear()
    world.messaging.sent.clear()
    taps = [
        make_callback(f"ok:{new.id}", sender=FRANCK, message_id=second_card, event_id=str(n))
        for n in (20, 21)
    ]
    await asyncio.gather(*(handle_inbound(tap, world.messaging, world.services) for tap in taps))
    notes = sorted(note or "" for _, note in world.messaging.answered)
    assert notes == sorted([messages.CB_STARTED, messages.CB_ALREADY])
    [issue] = world.codehost.issues.values()
    assert issue.title == "Checkout crashes on Android 13"
    assert issue.assignees == ("tekeufranck681",)  # the changed proposal ran, not the first
    assert "Requested by @tekeufranck681 via chirpo." in issue.body
    stored = world.services.pending.rows[new.id]  # type: ignore[attr-defined]
    assert stored.status == PendingStatus.EXECUTED
    # The card itself shows the result with the link, once (not a second message).
    links = [text for _, text in world.messaging.edited if issue.url in text]
    assert len(links) == 1
    assert world.messaging.buttons[second_card] == ()

    # 5. Everything that ran is audited: who asked, who confirmed, what, the result.
    [entry] = [e for e in world.audit.entries if e.action == "create_issue"]
    assert entry.result == AuditResult.EXECUTED
    assert entry.confirmed_by_member_id == entry.actor_member_id  # Franck asked and confirmed
    assert SANDBOX.full_name in str(entry.payload)
