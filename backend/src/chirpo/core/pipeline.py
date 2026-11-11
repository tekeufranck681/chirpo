"""Message -> interpret -> validate -> authorise -> confirm -> execute.

Commands are handled deterministically (core/commands); anything else addressed to the bot goes to
the interpreter (core/interpreter), which answers reads and posts a card for changes; a press on
the card's buttons goes to core/confirmation.
"""

import asyncio

from chirpo.core import messages
from chirpo.core.commands import COMMANDS, Request, Services
from chirpo.core.confirmation import handle_callback
from chirpo.core.domain import InboundCallback, InboundEvent, InboundMessage
from chirpo.core.interpreter import interpret
from chirpo.core.ports import MessagingAdapter, ProcessedEvents
from chirpo.core.router import is_explicitly_addressed, parse_command


async def handle_inbound(
    event: InboundEvent, messaging: MessagingAdapter, services: Services | None = None
) -> None:
    if isinstance(event, InboundCallback):
        await handle_callback(event, messaging, services)
        return
    if event.sender.is_bot:
        return  # bots never trigger the bot (no loops)
    if not event.text.strip():
        return  # media alone asks for nothing; the buffer still remembers it
    command = parse_command(event.text)
    if command is not None and (handler := COMMANDS.get(command.name)):
        await handler(Request(event, command.args, messaging, services))
        return
    if not is_explicitly_addressed(event):
        return  # unknown commands in a group may belong to another bot: stay quiet (D22)
    if command is not None:
        await messaging.send_message(
            event.chat, messages.UNKNOWN_COMMAND, reply_to=event.message_id
        )
        return
    await interpret(Request(event, event.text, messaging, services))


async def process(
    event: InboundEvent,
    messaging: MessagingAdapter,
    processed: ProcessedEvents,
    services: Services | None = None,
) -> bool:
    """Handle an event exactly once. Returns False for a duplicate delivery.

    The claim comes first: if handling then fails, the event is not retried, so a bot reply or an
    action is never repeated (at-most-once after the claim, D31). Only a job cancelled by a worker
    shutdown gives its claim back, since the queue runs it again.
    """
    if not await processed.claim(messaging.event_source, event.event_id):
        return False
    try:
        await handle_inbound(event, messaging, services)
    except asyncio.CancelledError:
        # The worker is stopping (deploy, restart) and the queue will retry this job: let the
        # retry handle it instead of skipping it as a duplicate. Errors still aren't retried.
        await asyncio.shield(processed.release(messaging.event_source, event.event_id))
        raise
    # After handling: a buffer failure can't cost the command, and a request never reads itself.
    await _remember(event, services)
    return True


async def _remember(event: InboundEvent, services: Services | None) -> None:
    """Keeps plain messages from linked chats as context for later requests (D46)."""
    buffer = services.buffer if services else None
    if buffer is None or services is None or not isinstance(event, InboundMessage):
        return
    if event.sender.is_bot or (not event.text and not event.attachments):
        return
    if event.text.startswith("/"):
        return  # commands, known or not, are no context
    workspace = await services.workspaces.for_chat(event.chat)
    if workspace is not None and workspace.keeps_context:
        await buffer.remember(workspace.id, event)
