"""Telegram adapter against saved real-format payloads and a mocked Bot API (no network)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from telegram import Bot

from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter
from chirpo.core.domain import ChatRef, InboundCallback, InboundMessage, SentMessage
from chirpo.core.errors import (
    MessagingError,
    MessagingForbiddenError,
    MessagingRateLimitedError,
    MessagingTimeoutError,
    MessagingUnavailableError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
TOKEN = "123:TEST"
API = f"https://api.telegram.org/bot{TOKEN}"
GROUP = ChatRef(platform="telegram", chat_id="-1001234567890")
SENT = {
    "message_id": 77,
    "date": 1762600000,
    "chat": {"id": -1001234567890, "type": "supergroup", "title": "chirpo test"},
    "text": "pong",
}


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def error_body(code: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


@pytest.fixture
def adapter() -> TelegramAdapter:
    return TelegramAdapter(Bot(TOKEN), bot_id=7000000001, bot_username="chirpo_test_bot")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def parse_message(adapter: TelegramAdapter, name: str) -> InboundMessage:
    event = adapter.parse_update(load(name))
    assert isinstance(event, InboundMessage)
    return event


# ------------------------------------------------------------------ parse_update


def test_group_command(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "group_command")
    assert message.text == "/ping"
    assert message.event_id == "900001"
    assert message.chat == GROUP
    assert message.sender.display_name == "Franck Tekeu"
    assert message.sender.user_id == "111111"
    assert not message.mentions_bot
    assert not message.is_private_chat
    assert message.sent_at.tzinfo is not None


def test_command_targeted_at_us_is_stripped_and_counts_as_mention(
    adapter: TelegramAdapter,
) -> None:
    message = parse_message(adapter, "group_command_for_us")
    assert message.text == "/ping"
    assert message.mentions_bot


def test_command_for_another_bot_is_dropped(adapter: TelegramAdapter) -> None:
    assert adapter.parse_update(load("group_command_for_other_bot")) is None


def test_mention_is_removed_case_insensitively(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "mention")
    assert message.mentions_bot
    assert message.text == "checkout crashes on Android, give it to Franck"


def test_reply_to_the_bot(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "reply_to_bot")
    assert message.replies_to_bot
    assert not message.mentions_bot


def test_plain_group_message(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "plain_group_message")
    assert not (message.mentions_bot or message.replies_to_bot or message.is_private_chat)


def test_private_chat(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "private_message").is_private_chat


def test_topic_messages_keep_their_thread(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "topic_message").chat.thread_id == "42"


def test_caption_counts_as_text(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "photo_with_caption")
    assert message.mentions_bot
    assert message.text == "this screen is broken"


def test_media_without_caption_is_ignored(adapter: TelegramAdapter) -> None:
    assert adapter.parse_update(load("photo_without_caption")) is None


def test_anonymous_admin_is_not_treated_as_a_bot(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "anonymous_admin")
    assert not message.sender.is_bot
    assert message.sender.user_id == "chat:-1001234567890"


def test_messages_from_bots_are_marked(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "message_from_bot").sender.is_bot


def test_edits_are_ignored(adapter: TelegramAdapter) -> None:
    assert adapter.parse_update(load("edited_message")) is None


def test_button_press(adapter: TelegramAdapter) -> None:
    event = adapter.parse_update(load("callback"))
    assert isinstance(event, InboundCallback)
    assert (event.data, event.message_id, event.chat) == ("confirm:abc", "14", GROUP)


# ------------------------------------------------------------------ sending


async def test_send_message_replies_without_link_previews(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    route = api.post("/sendMessage").respond(json={"ok": True, "result": SENT})
    sent = await adapter.send_message(GROUP, "pong", reply_to="1")

    assert sent == SentMessage(chat=GROUP, message_id="77")
    body = route.calls[0].request.content.decode()
    assert "chat_id=-1001234567890" in body
    assert "reply_parameters" in body
    assert "is_disabled" in body


async def test_send_into_a_topic(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/sendMessage").respond(json={"ok": True, "result": SENT})
    await adapter.send_message(GROUP.model_copy(update={"thread_id": "42"}), "hi")
    assert "message_thread_id=42" in route.calls[0].request.content.decode()


async def test_edit_message(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/editMessageText").respond(json={"ok": True, "result": SENT})
    await adapter.edit_message(SentMessage(chat=GROUP, message_id="77"), "done")
    assert "message_id=77" in route.calls[0].request.content.decode()


async def test_editing_to_the_same_text_is_not_an_error(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    api.post("/editMessageText").respond(
        400, json=error_body(400, "Bad Request: message is not modified")
    )
    await adapter.edit_message(SentMessage(chat=GROUP, message_id="77"), "same")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, error_body(403, "Forbidden: bot was kicked"), MessagingForbiddenError),
        (
            429,
            error_body(429, "Too Many Requests: retry after 7", retry_after=7),
            MessagingRateLimitedError,
        ),
        (400, error_body(400, "Bad Request: chat not found"), MessagingError),
        (502, error_body(502, "Bad Gateway"), MessagingError),
    ],
)
async def test_platform_errors_become_domain_errors(
    adapter: TelegramAdapter,
    api: respx.MockRouter,
    status: int,
    body: dict[str, Any],
    error: type[Exception],
) -> None:
    api.post("/sendMessage").respond(status, json=body)
    with pytest.raises(error):
        await adapter.send_message(GROUP, "pong")


async def test_rate_limit_carries_the_wait_time(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    api.post("/sendMessage").respond(429, json=error_body(429, "Too Many Requests", retry_after=7))
    with pytest.raises(MessagingRateLimitedError) as caught:
        await adapter.send_message(GROUP, "pong")
    assert caught.value.retry_after_seconds == 7


async def test_network_failure_is_retryable(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    api.post("/sendMessage").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(MessagingUnavailableError):
        await adapter.send_message(GROUP, "pong")


async def test_connect_learns_the_bot_identity(api: respx.MockRouter) -> None:
    me = {"id": 7000000001, "is_bot": True, "first_name": "Chirpo", "username": "chirpo_test_bot"}
    api.post("/getMe").respond(json={"ok": True, "result": me})
    adapter = await TelegramAdapter.connect(TOKEN)
    assert parse_message(adapter, "group_command_for_us").mentions_bot


# ------------------------------------------------------------------ review edge cases


@pytest.mark.parametrize("raw", [{}, {"update_id": 1, "message": {}}, {"update_id": "x"}])
def test_malformed_updates_are_ignored(adapter: TelegramAdapter, raw: dict[str, Any]) -> None:
    assert adapter.parse_update(raw) is None


def test_mention_inside_an_email_is_not_a_mention(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "mention_in_email")
    assert not message.mentions_bot
    assert "support@chirpo_test_bot.example" in message.text


def test_removing_a_mid_sentence_mention_keeps_text_tidy(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "mention_mid_sentence")
    assert message.mentions_bot
    assert message.text == "hey can you\nlist my issues"


def test_topic_root_is_not_a_reply_to_the_bot(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "topic_message_in_bot_topic")
    assert not message.replies_to_bot
    assert message.chat.thread_id == "20"


def test_anonymous_admin_is_marked_anonymous(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "anonymous_admin").sender.is_anonymous


@pytest.mark.parametrize("name", ["automatic_forward", "no_sender"])
def test_messages_without_a_real_person_are_dropped(adapter: TelegramAdapter, name: str) -> None:
    assert adapter.parse_update(load(name)) is None


def test_a_bot_without_username_is_refused() -> None:
    with pytest.raises(MessagingError):
        TelegramAdapter(Bot(TOKEN), bot_id=1, bot_username="")


async def test_timeout_is_not_reported_as_safe_to_retry(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    api.post("/sendMessage").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(MessagingTimeoutError) as caught:
        await adapter.send_message(GROUP, "pong")
    assert not isinstance(caught.value, MessagingUnavailableError)


async def test_aclose_shuts_the_bot_down(adapter: TelegramAdapter) -> None:
    await adapter.aclose()
