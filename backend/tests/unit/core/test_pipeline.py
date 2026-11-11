from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from chirpo.core import messages
from chirpo.core.commands import Services
from chirpo.core.domain import Attachment, AttachmentKind, ChatUser, InboundMessage
from chirpo.core.permissions import PermissionChecker
from chirpo.core.pipeline import handle_inbound, process
from tests.fakes import (
    CHAT,
    FakeAuditLog,
    FakeCodeHost,
    FakeMembers,
    FakeMessageBuffer,
    FakeMessaging,
    FakePendingActions,
    FakeProcessedEvents,
    FakeWorkspaces,
    make_callback,
    make_message,
)


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
    assert await _replies(text=command) == [messages.HELP.format(bot="@chirpo_bot")]


async def test_about_credits_the_creator_and_links_the_repo() -> None:
    (reply,) = await _replies(text="/about")
    assert "Tekeu Franck" in reply
    assert "github.com/tekeufranck681/chirpo" in reply


async def test_unknown_command_in_a_group_is_left_to_other_bots() -> None:
    assert await _replies(text="/ban someone") == []


@pytest.mark.parametrize("flag", ["mentions_bot", "replies_to_bot", "is_private_chat"])
async def test_unknown_command_meant_for_us_points_to_help(flag: str) -> None:
    assert await _replies(text="/frobnicate", **{flag: True}) == [messages.UNKNOWN_COMMAND]


async def test_a_plain_request_goes_to_the_interpreter() -> None:
    replies = await _replies(text="create an issue", mentions_bot=True)
    assert replies == [messages.GITHUB_NOT_CONFIGURED]  # no services in this test


async def test_mention_followed_by_a_command_runs_the_command() -> None:
    # The adapter strips "@chirpo", so "@chirpo /help" arrives as "/help" with mentions_bot.
    assert await _replies(text="/help", mentions_bot=True) == [
        messages.HELP.format(bot="@chirpo_bot")
    ]


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


async def test_a_message_cut_off_by_a_shutdown_is_handled_on_the_retry() -> None:
    """A worker stopping mid-job (deploy, restart) cancels it and the queue retries it; the
    retry must not be skipped as a duplicate, or the message is lost."""
    import asyncio

    class SlowMessaging(FakeMessaging):
        def __init__(self) -> None:
            super().__init__()
            self.slow = True

        async def send_message(self, chat: Any, text: str, *, reply_to: str | None = None) -> Any:
            if self.slow:
                await asyncio.sleep(10)
            return await super().send_message(chat, text, reply_to=reply_to)

    messaging, processed = SlowMessaging(), FakeProcessedEvents()
    message = make_message("/ping")
    job = asyncio.create_task(process(message, messaging, processed))
    await asyncio.sleep(0.01)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    messaging.slow = False
    assert await process(message, messaging, processed) is True
    assert [text for _, text, _ in messaging.sent] == [messages.PONG]


async def test_process_handles_an_event_once() -> None:
    from chirpo.core.pipeline import process
    from tests.fakes import FakeProcessedEvents

    messaging, processed = FakeMessaging(), FakeProcessedEvents()
    message = make_message("/ping")

    assert await process(message, messaging, processed) is True
    assert await process(message, messaging, processed) is False
    assert len(messaging.sent) == 1


class World:
    """A linked or unlinked chat, with a buffer, for the remembering tests (D46)."""

    def __init__(self) -> None:
        members = FakeMembers()
        codehost = FakeCodeHost()
        self.workspaces = FakeWorkspaces(members)
        self.buffer = FakeMessageBuffer(timedelta(hours=24))
        self.services = Services(
            workspaces=self.workspaces,
            members=members,
            audit=FakeAuditLog(),
            codehost=codehost,
            permissions=PermissionChecker(codehost),
            pending=FakePendingActions(),
            buffer=self.buffer,
            context_hours=24,
        )
        self.messaging = FakeMessaging()
        self.processed = FakeProcessedEvents()

    async def send(self, *sent: InboundMessage, buffer: bool = True) -> None:
        services = self.services if buffer else replace(self.services, buffer=None)
        for message in sent:
            await process(message, self.messaging, self.processed, services)

    def kept(self) -> list[str]:
        return [entry.text for entry in self.buffer.everything()]


async def _linked() -> World:
    world = World()
    await world.workspaces.get_or_create_for_chat(CHAT, name="Shop")
    return world


class TestRemembering:
    async def test_plain_messages_in_a_linked_chat(self) -> None:
        world = await _linked()
        await world.send(
            make_message("checkout crashes on Android", event_id="1", message_id="10"),
            make_message("assign it to Franck", event_id="2", message_id="11", mentions_bot=True),
        )
        assert world.kept() == ["checkout crashes on Android", "assign it to Franck"]

    async def test_not_in_an_unlinked_chat(self) -> None:
        world = World()
        await world.send(make_message("hello"))
        assert world.kept() == []

    async def test_not_commands(self) -> None:
        world = await _linked()
        await world.send(
            make_message("/iam", event_id="1", message_id="10"),
            make_message("/ping@chirpo_bot", event_id="2", message_id="11"),
            make_message("/foo bar", event_id="3", message_id="12"),
        )
        assert world.kept() == []

    async def test_a_photo_without_text_is_kept_with_its_attachment(self) -> None:
        world = await _linked()
        photo = Attachment(
            kind=AttachmentKind.IMAGE, file_id="P1", mime_type="image/jpeg", size=900
        )
        await world.send(make_message("", event_id="1", message_id="10", attachments=(photo,)))
        [kept] = world.buffer.everything()
        assert (kept.text, kept.attachments) == ("", (photo,))

    async def test_a_caption_and_its_photo_are_kept_together(self) -> None:
        world = await _linked()
        photo = Attachment(kind=AttachmentKind.IMAGE, file_id="P1", mime_type="image/jpeg")
        await world.send(
            make_message("crash on checkout", event_id="1", message_id="10", attachments=(photo,))
        )
        [kept] = world.buffer.everything()
        assert (kept.text, kept.attachments) == ("crash on checkout", (photo,))

    async def test_not_bots_or_empty_messages(self) -> None:
        world = await _linked()
        bot = ChatUser(platform="fake", user_id="9", display_name="CI", is_bot=True)
        await world.send(
            make_message("build failed", event_id="1", message_id="10", sender=bot),
            make_message("", event_id="2", message_id="11"),
        )
        assert world.kept() == []

    async def test_a_duplicate_delivery_is_not_remembered_twice(self) -> None:
        world = await _linked()
        same = make_message("checkout crashes")
        await world.send(same, same)
        assert world.kept() == ["checkout crashes"]

    async def test_without_a_buffer_nothing_is_kept_and_commands_still_work(self) -> None:
        world = await _linked()
        await world.send(
            make_message("/ping"), make_message("hi", event_id="2", mentions_bot=True), buffer=False
        )
        assert world.kept() == []
        assert len(world.messaging.sent) == 2

    async def test_a_failing_buffer_never_costs_the_command(self) -> None:
        class Broken(FakeMessageBuffer):
            async def remember(self, workspace_id: UUID, message: InboundMessage) -> None:
                raise RuntimeError("database down")

        world = await _linked()
        world.services = replace(world.services, buffer=Broken(timedelta(hours=24)))
        with pytest.raises(RuntimeError):
            await world.send(make_message("hi", mentions_bot=True))
        assert [text for _, text, _ in world.messaging.sent] == [messages.LLM_NOT_CONFIGURED]

    async def test_not_when_the_team_turned_context_off(self) -> None:
        world = await _linked()
        workspace = await world.workspaces.for_chat(CHAT)
        assert workspace is not None
        await world.workspaces.set_keeps_context(workspace.id, keep=False)
        await world.send(make_message("checkout crashes"))
        assert world.kept() == []
