"""The job that tidies cards (M5.4): old ones expire and say so, runs that were cut off are
failed and say so, and finished ones are deleted after a week."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from chirpo.core import messages
from chirpo.core.actions import CloseIssue, IssueRef
from chirpo.core.domain import ChatRef, RepoRef, SentMessage
from chirpo.core.errors import MessagingError
from chirpo.core.pending import PendingAction, PendingStatus, Step
from chirpo.core.sweep import KEEP_FINISHED, STUCK_AFTER, sweep
from tests.fakes import CHAT, FakeMessaging, FakePendingActions

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
REPO = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
STEP = Step.of(CloseIssue(repo=REPO.full_name, issue=IssueRef(number=7)), REPO)
WORKSPACE, ASKER = uuid4(), uuid4()


async def card(
    store: FakePendingActions,
    *,
    expires_in: timedelta = timedelta(hours=1),
    path: tuple[PendingStatus, ...] = (),
    touched: datetime = NOW,
    chat: ChatRef = CHAT,
) -> PendingAction:
    """A proposal with a card on message 77, walked through `path`, last touched at `touched`."""
    row = await store.create(
        workspace_id=WORKSPACE,
        requested_by=ASKER,
        chat=chat,
        steps=[STEP],
        expires_at=NOW + expires_in,
    )
    await store.attach_message(WORKSPACE, row.id, "77")
    current = PendingStatus.PROPOSED
    for to in path:
        await store.advance(WORKSPACE, row.id, expect=current, to=to, now=touched)
        current = to
    return await _fresh(store, row)


async def _fresh(store: FakePendingActions, row: PendingAction) -> PendingAction:
    found = await store.get(WORKSPACE, row.id)
    assert found is not None
    return found


def messengers() -> tuple[FakeMessaging, dict[str, FakeMessaging]]:
    messaging = FakeMessaging()
    return messaging, {CHAT.platform: messaging}


async def test_an_old_card_expires_and_says_so() -> None:
    store = FakePendingActions()
    row = await card(store, expires_in=timedelta(minutes=-1))
    messaging, adapters = messengers()
    swept = await sweep(store, adapters, NOW)
    assert (await _fresh(store, row)).status == PendingStatus.EXPIRED
    assert messaging.edited == [(SentMessage(chat=CHAT, message_id="77"), messages.EXPIRED)]
    assert messaging.buttons["77"] == ()  # nothing left to press
    assert swept.expired == 1


async def test_a_card_that_still_has_time_is_left_alone() -> None:
    store = FakePendingActions()
    row = await card(store, expires_in=timedelta(minutes=30))
    messaging, adapters = messengers()
    await sweep(store, adapters, NOW)
    assert (await _fresh(store, row)).status == PendingStatus.PROPOSED
    assert messaging.edited == []


async def test_a_run_cut_off_for_a_quarter_of_an_hour_is_failed_and_says_so() -> None:
    store = FakePendingActions()
    cut_off = NOW - STUCK_AFTER - timedelta(minutes=1)
    stuck = await card(
        store, path=(PendingStatus.CONFIRMED, PendingStatus.EXECUTING), touched=cut_off
    )
    half = await card(store, path=(PendingStatus.CONFIRMED,), touched=cut_off)
    messaging, adapters = messengers()
    swept = await sweep(store, adapters, NOW)
    assert (await _fresh(store, stuck)).status == PendingStatus.FAILED
    assert (await _fresh(store, half)).status == PendingStatus.FAILED
    assert [text for _, text in messaging.edited] == [messages.CRASHED, messages.CRASHED]
    assert swept.failed == 2


async def test_a_run_that_is_only_slow_is_not_failed() -> None:
    store = FakePendingActions()
    recent = NOW - STUCK_AFTER + timedelta(minutes=5)
    row = await card(store, path=(PendingStatus.CONFIRMED, PendingStatus.EXECUTING), touched=recent)
    messaging, adapters = messengers()
    await sweep(store, adapters, NOW)
    assert (await _fresh(store, row)).status == PendingStatus.EXECUTING
    assert messaging.edited == []


async def test_finished_rows_are_deleted_after_a_week_and_only_then() -> None:
    store = FakePendingActions()
    week_old = NOW - KEEP_FINISHED - timedelta(minutes=1)
    old = await card(store, path=(PendingStatus.REJECTED,), touched=week_old)
    young = await card(store, path=(PendingStatus.REJECTED,), touched=NOW - timedelta(days=2))
    waiting = await card(store)
    _, adapters = messengers()
    swept = await sweep(store, adapters, NOW)
    assert await store.get(WORKSPACE, old.id) is None
    assert await store.get(WORKSPACE, young.id) is not None
    assert await store.get(WORKSPACE, waiting.id) is not None
    assert swept.purged == 1


async def test_a_card_that_can_not_be_edited_does_not_stop_the_others() -> None:
    store = FakePendingActions()
    first = await card(store, expires_in=timedelta(minutes=-1))
    second = await card(store, expires_in=timedelta(minutes=-1))
    messaging, adapters = messengers()
    calls = 0
    real = messaging.edit_message

    async def first_fails(message: SentMessage, text: str, **kw: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MessagingError("message to edit not found")
        await real(message, text)

    messaging.edit_message = first_fails  # type: ignore[method-assign]
    await sweep(store, adapters, NOW)
    assert calls == 2
    assert (await _fresh(store, first)).status == PendingStatus.EXPIRED
    assert (await _fresh(store, second)).status == PendingStatus.EXPIRED


async def test_a_platform_without_an_adapter_is_skipped() -> None:
    store = FakePendingActions()
    row = await card(store, expires_in=timedelta(minutes=-1))
    swept = await sweep(store, {}, NOW)
    assert (await _fresh(store, row)).status == PendingStatus.EXPIRED  # still recorded
    assert swept.expired == 1


async def test_a_card_that_was_never_sent_has_nothing_to_edit() -> None:
    store = FakePendingActions()
    row = await store.create(
        workspace_id=WORKSPACE,
        requested_by=ASKER,
        chat=CHAT,
        steps=[STEP],
        expires_at=NOW - timedelta(minutes=1),
    )
    messaging, adapters = messengers()
    await sweep(store, adapters, NOW)
    assert (await _fresh(store, row)).status == PendingStatus.EXPIRED
    assert messaging.edited == []


async def test_any_error_editing_one_card_still_lets_the_rest_be_told() -> None:
    store = FakePendingActions()
    expired = await card(store, expires_in=timedelta(minutes=-1))
    cut_off = NOW - STUCK_AFTER - timedelta(minutes=1)
    stuck = await card(store, path=(PendingStatus.CONFIRMED,), touched=cut_off)
    messaging, adapters = messengers()
    real = messaging.edit_message
    calls = 0

    async def first_blows_up(message: SentMessage, text: str, **kw: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("something the adapter didn't translate")
        await real(message, text)

    messaging.edit_message = first_blows_up  # type: ignore[method-assign]
    swept = await sweep(store, adapters, NOW)
    assert calls == 2  # the second kind of card was still told
    assert (swept.expired, swept.failed) == (1, 1)
    assert (await _fresh(store, expired)).status == PendingStatus.EXPIRED
    assert (await _fresh(store, stuck)).status == PendingStatus.FAILED
