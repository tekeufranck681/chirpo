"""Telegram adapter against saved real-format payloads and a mocked Bot API (no network)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from telegram import Bot

from chirpo.adapters.messaging.telegram.adapter import TelegramAdapter
from chirpo.core.domain import (
    Attachment,
    AttachmentKind,
    Button,
    ChatRef,
    ChatUser,
    InboundCallback,
    InboundMessage,
    SentMessage,
)
from chirpo.core.errors import (
    AttachmentTooLargeError,
    AttachmentUnavailableError,
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
    replied = load("reply_to_bot")["message"]["reply_to_message"]["message_id"]
    assert message.reply_to_message_id == str(replied)


def test_plain_group_message(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "plain_group_message")
    assert not (message.mentions_bot or message.replies_to_bot or message.is_private_chat)
    assert message.reply_to_message_id is None


def test_private_chat(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "private_message").is_private_chat


def test_topic_messages_keep_their_thread(adapter: TelegramAdapter) -> None:
    assert parse_message(adapter, "topic_message").chat.thread_id == "42"


def test_caption_counts_as_text(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "photo_with_caption")
    assert message.mentions_bot
    assert message.text == "this screen is broken"


def test_a_photo_without_caption_is_a_message_with_no_text(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "photo_without_caption")
    assert message.text == ""
    assert not message.mentions_bot
    assert message.attachments == (
        Attachment(kind=AttachmentKind.IMAGE, file_id="AgAC", mime_type="image/jpeg", size=None),
    )


def test_a_photo_is_its_largest_size(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "photo_sizes")
    assert message.text == "see this"
    assert message.attachments == (
        Attachment(kind=AttachmentKind.IMAGE, file_id="L", mime_type="image/jpeg", size=150000),
    )


def test_an_image_sent_as_a_file_keeps_its_type_and_size(adapter: TelegramAdapter) -> None:
    [attachment] = parse_message(adapter, "document_png").attachments
    assert attachment == Attachment(
        kind=AttachmentKind.IMAGE, file_id="DOC-crash.png", mime_type="image/png", size=245123
    )


def test_an_svg_is_an_image_by_type_and_left_to_the_core_to_refuse(
    adapter: TelegramAdapter,
) -> None:
    [attachment] = parse_message(adapter, "document_svg").attachments
    assert (attachment.kind, attachment.mime_type) == (AttachmentKind.IMAGE, "image/svg+xml")


def test_other_files_and_voice_messages_are_kept_as_what_they_are(
    adapter: TelegramAdapter,
) -> None:
    [pdf] = parse_message(adapter, "document_pdf").attachments
    assert (pdf.kind, pdf.mime_type) == (AttachmentKind.FILE, "application/pdf")
    voice = parse_message(adapter, "voice")
    assert voice.text == ""
    assert [a.kind for a in voice.attachments] == [AttachmentKind.VOICE]


def test_a_text_message_has_no_attachments(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "mention")
    assert message.attachments == ()
    assert message.reply_to_attachments == ()


def test_the_photo_being_replied_to_comes_with_the_reply(adapter: TelegramAdapter) -> None:
    message = parse_message(adapter, "reply_to_photo")
    assert message.attachments == ()
    assert message.reply_to_message_id == "5"
    assert message.reply_to_attachments == (
        Attachment(
            kind=AttachmentKind.IMAGE, file_id="REPLIED", mime_type="image/jpeg", size=54321
        ),
    )


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
    assert event.query_id == "4382bfdwdsb323b2d9"  # what answering the press needs
    assert not event.sender.is_anonymous  # a press always comes from a person's account


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


async def test_buttons_go_under_the_message_in_one_row(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    route = api.post("/sendMessage").respond(json={"ok": True, "result": SENT})
    buttons = [Button(text="Yes", data="ok:1"), Button(text="No", data="no:1")]
    await adapter.send_message(GROUP, "Sure?", buttons=buttons)
    markup = json.loads(dict(parse_qsl(route.calls[0].request.content.decode()))["reply_markup"])
    assert markup == {
        "inline_keyboard": [
            [{"text": "Yes", "callback_data": "ok:1"}, {"text": "No", "callback_data": "no:1"}]
        ]
    }


async def test_a_message_without_buttons_has_no_keyboard(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    route = api.post("/sendMessage").respond(json={"ok": True, "result": SENT})
    await adapter.send_message(GROUP, "pong")
    assert "reply_markup" not in route.calls[0].request.content.decode()


async def test_send_into_a_topic(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/sendMessage").respond(json={"ok": True, "result": SENT})
    await adapter.send_message(GROUP.model_copy(update={"thread_id": "42"}), "hi")
    assert "message_thread_id=42" in route.calls[0].request.content.decode()


async def test_show_typing_in_a_topic(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/sendChatAction").respond(json={"ok": True, "result": True})
    await adapter.show_typing(GROUP.model_copy(update={"thread_id": "42"}))
    sent = route.calls[0].request.content.decode()
    assert "action=typing" in sent
    assert "message_thread_id=42" in sent


def test_the_bot_is_mentioned_by_its_username(adapter: TelegramAdapter) -> None:
    assert adapter.bot_mention() == "@chirpo_test_bot"


async def test_edit_message(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/editMessageText").respond(json={"ok": True, "result": SENT})
    await adapter.edit_message(SentMessage(chat=GROUP, message_id="77"), "done")
    assert "message_id=77" in route.calls[0].request.content.decode()


async def test_editing_without_buttons_sends_no_keyboard_so_telegram_removes_it(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    route = api.post("/editMessageText").respond(json={"ok": True, "result": SENT})
    await adapter.edit_message(SentMessage(chat=GROUP, message_id="77"), "done")
    assert "reply_markup" not in route.calls[0].request.content.decode()  # Telegram drops them


async def test_answering_a_press(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    route = api.post("/answerCallbackQuery").respond(json={"ok": True, "result": True})
    event = adapter.parse_update(load("callback"))
    assert isinstance(event, InboundCallback)
    await adapter.answer_callback(event, "Only Franck can confirm this.")
    body = dict(parse_qsl(route.calls[0].request.content.decode()))
    assert body["callback_query_id"] == "4382bfdwdsb323b2d9"
    assert body["text"] == "Only Franck can confirm this."


async def test_answering_a_stale_press_is_an_error_the_core_can_ignore(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    api.post("/answerCallbackQuery").respond(
        400, json=error_body(400, "Bad Request: query is too old and response timeout expired")
    )
    event = adapter.parse_update(load("callback"))
    assert isinstance(event, InboundCallback)
    with pytest.raises(MessagingError):
        await adapter.answer_callback(event)


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
    assert message.reply_to_message_id is None
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


async def test_fetch_updates_long_polls_from_the_offset(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    route = api.post("/getUpdates").respond(json={"ok": True, "result": [load("group_command")]})
    updates = await adapter.fetch_updates(offset=900001, wait_seconds=30)

    assert [u["update_id"] for u in updates] == [900001]
    body = route.calls[0].request.content.decode()
    assert "offset=900001" in body
    assert "timeout=30" in body
    assert adapter.parse_update(updates[0]) is not None


def test_event_source_names_the_bot(adapter: TelegramAdapter) -> None:
    assert adapter.event_source == "telegram:7000000001"


async def test_set_webhook_sends_url_secret_and_update_types(
    adapter: TelegramAdapter, api: respx.MockRouter
) -> None:
    from urllib.parse import parse_qs

    route = api.post("/setWebhook").respond(json={"ok": True, "result": True})
    url = "https://example.trycloudflare.com/webhooks/telegram"
    await adapter.set_webhook(url, "s3cret")

    sent = parse_qs(route.calls[0].request.content.decode())
    assert sent["url"] == [url]
    assert sent["secret_token"] == ["s3cret"]
    assert json.loads(sent["allowed_updates"][0]) == ["message", "callback_query"]


async def test_delete_webhook_and_info(adapter: TelegramAdapter, api: respx.MockRouter) -> None:
    api.post("/deleteWebhook").respond(json={"ok": True, "result": True})
    api.post("/getWebhookInfo").respond(
        json={
            "ok": True,
            "result": {"url": "", "has_custom_certificate": False, "pending_update_count": 3},
        }
    )
    await adapter.delete_webhook()
    assert (await adapter.webhook_info())["pending_update_count"] == 3


@pytest.mark.parametrize(
    ("status", "is_admin"),
    [("creator", True), ("administrator", True), ("member", False), ("left", False)],
)
async def test_group_admins_are_read_from_telegram(
    adapter: TelegramAdapter, api: respx.MockRouter, status: str, is_admin: bool
) -> None:
    user = {"id": 42, "is_bot": False, "first_name": "Franck"}
    member = {"status": status, "user": user}
    if status == "administrator":
        member |= {
            "can_be_edited": False,
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_manage_video_chats": True,
            "can_restrict_members": True,
            "can_promote_members": False,
            "can_change_info": True,
            "can_invite_users": True,
            "can_post_stories": False,
            "can_edit_stories": False,
            "can_delete_stories": False,
        }
    if status == "creator":
        member |= {"is_anonymous": False}
    route = api.post("/getChatMember").respond(json={"ok": True, "result": member})
    sender = ChatUser(platform="telegram", user_id="42", display_name="Franck")

    assert await adapter.is_chat_admin(GROUP, sender) is is_admin
    assert dict(parse_qsl(route.calls.last.request.content.decode())) == {
        "chat_id": "-1001234567890",
        "user_id": "42",
    }


# ------------------------------------------------------------------ downloading attachments

FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}"
PHOTO = Attachment(kind=AttachmentKind.IMAGE, file_id="AgAC", mime_type="image/jpeg", size=900)
UNSIZED = PHOTO.model_copy(update={"size": None})


@pytest.fixture
def bot() -> Iterator[respx.MockRouter]:
    """The Bot API and the file host, which has its own path."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def file_info(size: int | None = 900, path: str = "photos/file_1.jpg") -> dict[str, Any]:
    result: dict[str, Any] = {"file_id": "AgAC", "file_unique_id": "AQAD", "file_path": path}
    if size is not None:
        result["file_size"] = size
    return {"ok": True, "result": result}


async def test_an_attachment_is_downloaded(adapter: TelegramAdapter, bot: respx.MockRouter) -> None:
    lookup = bot.post(f"{API}/getFile").respond(json=file_info())
    bot.get(f"{FILE_URL}/photos/file_1.jpg").respond(content=b"\xff\xd8\xff-image")
    data = await adapter.download(PHOTO, max_bytes=1000)
    assert data == b"\xff\xd8\xff-image"
    assert "file_id=AgAC" in lookup.calls[0].request.content.decode()


async def test_a_known_size_over_the_limit_is_refused_without_calling_telegram(
    adapter: TelegramAdapter, bot: respx.MockRouter
) -> None:
    lookup = bot.post(f"{API}/getFile").respond(json=file_info())
    with pytest.raises(AttachmentTooLargeError):
        await adapter.download(PHOTO, max_bytes=500)
    assert not lookup.called


async def test_the_size_telegram_reports_is_checked_before_downloading(
    adapter: TelegramAdapter, bot: respx.MockRouter
) -> None:
    bot.post(f"{API}/getFile").respond(json=file_info(size=5_000_000))
    download = bot.get(f"{FILE_URL}/photos/file_1.jpg").respond(content=b"x")
    with pytest.raises(AttachmentTooLargeError):
        await adapter.download(UNSIZED, max_bytes=1000)
    assert not download.called


async def test_a_file_bigger_than_it_claimed_is_refused(
    adapter: TelegramAdapter, bot: respx.MockRouter
) -> None:
    bot.post(f"{API}/getFile").respond(json=file_info(size=None))
    bot.get(f"{FILE_URL}/photos/file_1.jpg").respond(content=b"x" * 2000)
    with pytest.raises(AttachmentTooLargeError):
        await adapter.download(UNSIZED, max_bytes=1000)


@pytest.mark.parametrize(
    ("description", "error"),
    [
        ("Bad Request: file is too big", AttachmentTooLargeError),
        (
            "Bad Request: wrong file_id or the file is temporarily unavailable",
            AttachmentUnavailableError,
        ),
        ("Bad Request: invalid file_id", AttachmentUnavailableError),
    ],
)
async def test_telegram_refusals_become_attachment_errors(
    adapter: TelegramAdapter,
    bot: respx.MockRouter,
    description: str,
    error: type[MessagingError],
) -> None:
    bot.post(f"{API}/getFile").respond(400, json=error_body(400, description))
    with pytest.raises(error):
        await adapter.download(PHOTO, max_bytes=1000)


async def test_a_network_failure_while_downloading_is_retryable(
    adapter: TelegramAdapter, bot: respx.MockRouter
) -> None:
    bot.post(f"{API}/getFile").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(MessagingUnavailableError):
        await adapter.download(PHOTO, max_bytes=1000)


async def test_attachment_errors_never_repeat_ids_or_telegrams_text(
    adapter: TelegramAdapter, bot: respx.MockRouter
) -> None:
    bot.post(f"{API}/getFile").respond(
        400, json=error_body(400, "Bad Request: wrong file_id AgAC or the file is gone")
    )
    with pytest.raises(AttachmentUnavailableError) as unavailable:
        await adapter.download(PHOTO, max_bytes=1000)
    assert "AgAC" not in str(unavailable.value)
    with pytest.raises(AttachmentTooLargeError) as large:
        await adapter.download(PHOTO, max_bytes=100)
    assert "AgAC" not in str(large.value)
