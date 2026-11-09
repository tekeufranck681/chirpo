"""Telegram adapter: normalises updates for the core, sends and edits messages.

Uses python-telegram-bot's `Bot` and `Update` classes only, not its Application framework (D31).
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Self

from telegram import Bot, LinkPreviewOptions, Message, ReplyParameters, Update, User
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from chirpo.core.domain import (
    ChatRef,
    ChatUser,
    InboundCallback,
    InboundEvent,
    InboundMessage,
    SentMessage,
)
from chirpo.core.errors import (
    MessagingError,
    MessagingForbiddenError,
    MessagingRateLimitedError,
    MessagingTimeoutError,
    MessagingUnavailableError,
)
from chirpo.logging import get_logger

PLATFORM = "telegram"
ALLOWED_UPDATES = ("message", "callback_query")
TELEGRAM_SERVICE_USER_ID = 777000  # posts auto-forwarded from a linked channel
LEADING_COMMAND = re.compile(r"^(?P<command>/[A-Za-z0-9_]{1,32})@(?P<target>[A-Za-z0-9_]+)")
REPEATED_SPACES = re.compile(r"[ \t]{2,}")

log = get_logger(__name__)


class TelegramAdapter:
    platform = PLATFORM

    def __init__(self, bot: Bot, *, bot_id: int, bot_username: str) -> None:
        if not bot_username:
            raise MessagingError("the Telegram bot has no username")
        self._bot = bot
        self._bot_id = bot_id
        self.event_source = f"{PLATFORM}:{bot_id}"
        self._bot_username = bot_username
        self._mention = re.compile(rf"(?<!\w)@{re.escape(bot_username)}\b", re.IGNORECASE)

    @classmethod
    async def connect(cls, token: str) -> Self:
        """Create the adapter, asking Telegram for the bot's own id and username (getMe)."""
        bot = Bot(token)
        me = await _translate_errors(bot.get_me)
        return cls(bot, bot_id=me.id, bot_username=me.username or "")

    @property
    def bot_id(self) -> int:
        return self._bot_id

    async def fetch_updates(self, offset: int | None, wait_seconds: int) -> list[dict[str, Any]]:
        """Long polling: Telegram holds the request up to `wait_seconds` until updates arrive."""
        updates = await _translate_errors(
            lambda: self._bot.get_updates(
                offset=offset, timeout=wait_seconds, allowed_updates=list(ALLOWED_UPDATES)
            )
        )
        return [update.to_dict() for update in updates]

    async def set_webhook(self, url: str, secret: str) -> None:
        await _translate_errors(
            lambda: self._bot.set_webhook(
                url=url, secret_token=secret, allowed_updates=list(ALLOWED_UPDATES)
            )
        )

    async def delete_webhook(self) -> None:
        await _translate_errors(self._bot.delete_webhook)

    async def webhook_info(self) -> dict[str, Any]:
        info = await _translate_errors(self._bot.get_webhook_info)
        return {
            "url": info.url,
            "pending_update_count": info.pending_update_count,
            "last_error_message": info.last_error_message,
        }

    async def aclose(self) -> None:
        await self._bot.shutdown()

    def parse_update(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        try:
            update = Update.de_json(dict(raw), self._bot)
        except (KeyError, TypeError, ValueError):
            # Never log the payload: it is untrusted user content.
            log.warning("telegram_update_malformed", update_id=raw.get("update_id"))
            return None
        if update.callback_query is not None:
            return self._parse_callback(update)
        if update.message is not None:
            return self._parse_message(update.update_id, update.message)
        return None  # edits, channel posts, member updates, ...: not handled

    async def send_message(
        self, chat: ChatRef, text: str, *, reply_to: str | None = None
    ) -> SentMessage:
        sent = await _translate_errors(
            lambda: self._bot.send_message(
                chat_id=int(chat.chat_id),
                text=text,
                message_thread_id=int(chat.thread_id) if chat.thread_id else None,
                reply_parameters=(
                    ReplyParameters(message_id=int(reply_to), allow_sending_without_reply=True)
                    if reply_to
                    else None
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        return SentMessage(chat=chat, message_id=str(sent.message_id))

    def private_chat(self, user: ChatUser) -> ChatRef:
        return ChatRef(platform=self.platform, chat_id=user.user_id)  # Telegram: same id

    def private_chat_link(self) -> str:
        return f"https://t.me/{self._bot_username}?start=iam"

    def bot_mention(self) -> str:
        return f"@{self._bot_username}"

    async def is_chat_admin(self, chat: ChatRef, user: ChatUser) -> bool:
        member = await _translate_errors(
            lambda: self._bot.get_chat_member(chat_id=int(chat.chat_id), user_id=int(user.user_id))
        )
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)

    async def show_typing(self, chat: ChatRef) -> None:
        await _translate_errors(
            lambda: self._bot.send_chat_action(
                chat_id=int(chat.chat_id),
                action=ChatAction.TYPING,
                message_thread_id=int(chat.thread_id) if chat.thread_id else None,
            )
        )

    async def edit_message(self, message: SentMessage, text: str) -> None:
        try:
            await _translate_errors(
                lambda: self._bot.edit_message_text(
                    chat_id=int(message.chat.chat_id),
                    message_id=int(message.message_id),
                    text=text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
        except MessagingError as error:
            if "message is not modified" not in str(error).lower():
                raise

    def _parse_message(self, update_id: int, message: Message) -> InboundMessage | None:
        text = message.text or message.caption or ""
        if not text.strip() or message.is_automatic_forward:
            return None
        sender = _sender(message)
        if sender is None:
            return None
        text, targeted = self._strip_command_target(text)
        if targeted is False:
            return None  # a command meant for another bot
        mentions_bot = bool(targeted) or bool(self._mention.search(text))
        text = REPEATED_SPACES.sub(" ", self._mention.sub("", text)).strip()
        replied = message.reply_to_message
        if replied is not None and replied.message_id == message.message_thread_id:
            replied = None  # in a topic, Telegram links every message to the topic's first one
        return InboundMessage(
            event_id=str(update_id),
            chat=_chat_ref(message),
            sender=sender,
            message_id=str(message.message_id),
            text=text,
            sent_at=message.date,
            mentions_bot=mentions_bot,
            replies_to_bot=bool(
                replied and replied.from_user and replied.from_user.id == self._bot_id
            ),
            reply_to_message_id=str(replied.message_id) if replied else None,
            is_private_chat=message.chat.type == "private",
        )

    def _parse_callback(self, update: Update) -> InboundCallback | None:
        query = update.callback_query
        if query is None or query.data is None or not isinstance(query.message, Message):
            return None
        return InboundCallback(
            event_id=str(update.update_id),
            chat=_chat_ref(query.message),
            sender=_user(query.from_user),
            message_id=str(query.message.message_id),
            data=query.data,
        )

    def _strip_command_target(self, text: str) -> tuple[str, bool | None]:
        """Handle "/cmd@bot". Returns (text, True) for us, (text, False) for another bot,
        (text, None) when there is no target."""
        match = LEADING_COMMAND.match(text)
        if match is None:
            return text, None
        if match.group("target").lower() != self._bot_username.lower():
            return text, False
        return match.group("command") + text[match.end() :], True


def _chat_ref(message: Message) -> ChatRef:
    thread = message.message_thread_id if message.is_topic_message else None
    return ChatRef(
        platform=PLATFORM,
        chat_id=str(message.chat.id),
        thread_id=str(thread) if thread is not None else None,
    )


def _user(user: User) -> ChatUser:
    return ChatUser(
        platform=PLATFORM,
        user_id=str(user.id),
        display_name=user.full_name,
        username=user.username,
        is_bot=user.is_bot,
    )


def _sender(message: Message) -> ChatUser | None:
    # Anonymous group admins post "as the chat": usable, but anonymous (never privileged).
    if message.sender_chat is not None:
        return ChatUser(
            platform=PLATFORM,
            user_id=f"chat:{message.sender_chat.id}",
            display_name=message.sender_chat.title or "anonymous",
            is_anonymous=True,
        )
    if message.from_user is None or message.from_user.id == TELEGRAM_SERVICE_USER_ID:
        return None
    return _user(message.from_user)


async def _translate_errors[T](call: Callable[[], Awaitable[T]]) -> T:
    try:
        return await call()
    except RetryAfter as error:
        retry = error.retry_after  # int in PTB 22, timedelta from PTB 23: both handled
        seconds = retry if isinstance(retry, int | float) else retry.total_seconds()
        raise MessagingRateLimitedError(seconds) from error
    except Forbidden as error:
        raise MessagingForbiddenError(str(error)) from error
    except TimedOut as error:
        raise MessagingTimeoutError(str(error)) from error
    except NetworkError as error:
        if isinstance(error, BadRequest):
            raise MessagingError(str(error)) from error
        raise MessagingUnavailableError(str(error)) from error
    except TelegramError as error:
        raise MessagingError(str(error)) from error
