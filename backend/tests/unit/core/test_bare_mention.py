"""A mention with no words (live testing, after M6.6): on a reply it means "deal with that
message", with a file it means "deal with this"; alone it gets a hint instead of silence."""

import logging
from datetime import UTC, datetime

import pytest

from chirpo.core import messages
from chirpo.core.actions import CreateIssue
from chirpo.core.domain import Attachment, AttachmentKind
from chirpo.core.errors import MessagingError
from chirpo.core.pipeline import handle_inbound
from tests.fakes import CHAT, make_message
from tests.unit.core.test_interpreter import ANNIE, World

LOGIN_BUG = "the login button does nothing on Safari"


async def remembered(world: World, text: str, message_id: str = "50") -> None:
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    said = make_message(text, sender=ANNIE, message_id=message_id, sent_at=datetime.now(UTC))
    await world.buffer.remember(workspace.id, said)


async def test_a_mention_alone_on_a_reply_handles_the_replied_message() -> None:
    world = World([CreateIssue(title="Login button does nothing on Safari")])
    await world.linked()
    await remembered(world, LOGIN_BUG)
    text = await world.say("", reply_to_message_id="50")
    assert text.startswith(messages.DRY_RUN_HEADER)
    [request] = world.llm.requests
    assert LOGIN_BUG in request.message  # the replied message, as context
    assert "Handle the message I'm replying to." in request.message
    assert world.messaging.typing  # "typing…" while the model thinks


async def test_a_mention_alone_with_a_picture_handles_the_picture() -> None:
    world = World([CreateIssue(title="Crash dialog on checkout")])
    await world.linked()
    image = Attachment(kind=AttachmentKind.IMAGE, file_id="F1", mime_type="image/png", size=10)
    await world.say("", attachments=(image,))
    [request] = world.llm.requests
    assert "Handle what I attached." in request.message


async def test_a_mention_alone_gets_a_hint_not_silence() -> None:
    world = World()
    await world.linked()
    text = await world.say("")
    assert text == messages.MENTION_HINT.format(bot=world.messaging.bot_mention())
    assert world.llm.requests == []


async def test_a_message_with_no_words_and_no_mention_still_asks_nothing() -> None:
    world = World()
    await world.linked()
    world.messaging.sent.clear()
    message = make_message("", mentions_bot=False, reply_to_message_id="50")
    await handle_inbound(message, world.messaging, world.services)
    assert world.messaging.sent == []


async def test_a_typing_indicator_that_fails_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    world = World([CreateIssue(title="Login button does nothing on Safari")])
    await world.linked()

    async def broken(*_: object) -> None:
        raise MessagingError("not enough rights")

    world.messaging.show_typing = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        text = await world.say(LOGIN_BUG)
    assert text.startswith(messages.DRY_RUN_HEADER)  # the reply still comes
    assert "typing_failed error=MessagingError" in caplog.text
    assert "not enough rights" not in caplog.text  # no platform text in the logs
