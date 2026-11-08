from datetime import datetime

import pytest
from pydantic import ValidationError

from chirpo.core import messages
from chirpo.core.domain import ChatUser
from chirpo.core.pipeline import handle_inbound
from tests.fakes import FakeMessaging, make_callback, make_message


async def _replies(**message_fields: object) -> list[str]:
    messaging = FakeMessaging()
    text = str(message_fields.pop("text"))
    await handle_inbound(make_message(text, **message_fields), messaging)
    return [sent_text for _, sent_text, _ in messaging.sent]


async def test_ping_replies_pong_to_the_same_message() -> None:
    messaging = FakeMessaging()
    message = make_message("/ping")
    await handle_inbound(message, messaging)
    assert messaging.sent == [(message.chat, messages.PONG, message.message_id)]


@pytest.mark.parametrize("command", ["/help", "/start"])
async def test_help_and_start_list_the_commands(command: str) -> None:
    assert await _replies(text=command) == [messages.HELP]


async def test_about_credits_the_creator_and_links_the_repo() -> None:
    (reply,) = await _replies(text="/about")
    assert "Tekeu Franck" in reply
    assert "github.com/tekeufranck681/chirpo" in reply


async def test_unknown_command_in_a_group_is_left_to_other_bots() -> None:
    assert await _replies(text="/ban someone") == []


@pytest.mark.parametrize("flag", ["mentions_bot", "replies_to_bot", "is_private_chat"])
async def test_unknown_command_meant_for_us_points_to_help(flag: str) -> None:
    assert await _replies(text="/frobnicate", **{flag: True}) == [messages.UNKNOWN_COMMAND]


async def test_mention_without_command_gets_a_not_yet_reply() -> None:
    replies = await _replies(text="create an issue", mentions_bot=True)
    assert replies == [messages.NOT_UNDERSTOOD_YET]


async def test_mention_followed_by_a_command_runs_the_command() -> None:
    # The adapter strips "@chirpo", so "@chirpo /help" arrives as "/help" with mentions_bot.
    assert await _replies(text="/help", mentions_bot=True) == [messages.HELP]


async def test_plain_group_chatter_is_ignored() -> None:
    assert await _replies(text="lunch at 1?") == []


async def test_messages_from_bots_are_ignored() -> None:
    bot = ChatUser(platform="fake", user_id="9", display_name="Other bot", is_bot=True)
    assert await _replies(text="/ping", sender=bot, is_private_chat=True) == []


async def test_button_presses_are_ignored_for_now() -> None:
    messaging = FakeMessaging()
    await handle_inbound(make_callback("confirm:1"), messaging)
    assert messaging.sent == []


def test_timestamps_must_carry_a_timezone() -> None:
    with pytest.raises(ValidationError):
        make_message("/ping", sent_at=datetime(2026, 1, 1))


async def test_process_handles_an_event_once() -> None:
    from chirpo.core.pipeline import process
    from tests.fakes import FakeProcessedEvents

    messaging, processed = FakeMessaging(), FakeProcessedEvents()
    message = make_message("/ping")

    assert await process(message, messaging, processed) is True
    assert await process(message, messaging, processed) is False
    assert len(messaging.sent) == 1
