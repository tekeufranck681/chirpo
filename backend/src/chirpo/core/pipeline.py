"""Message -> interpret -> validate -> authorise -> confirm -> execute.

M3 handles deterministic commands (core/commands); interpretation by the LLM arrives in M4.
"""

from chirpo.core import messages
from chirpo.core.commands import COMMANDS, Request, Services
from chirpo.core.domain import InboundEvent, InboundMessage
from chirpo.core.ports import MessagingAdapter, ProcessedEvents
from chirpo.core.router import is_explicitly_addressed, parse_command


async def handle_inbound(
    event: InboundEvent, messaging: MessagingAdapter, services: Services | None = None
) -> None:
    if not isinstance(event, InboundMessage) or event.sender.is_bot:
        return  # button presses are handled from M5; bots never trigger the bot (no loops)
    command = parse_command(event.text)
    if command is not None and (handler := COMMANDS.get(command.name)):
        await handler(Request(event, command.args, messaging, services))
        return
    if not is_explicitly_addressed(event):
        return  # unknown commands in a group may belong to another bot: stay quiet (D22)
    text = messages.UNKNOWN_COMMAND if command is not None else messages.NOT_UNDERSTOOD_YET
    await messaging.send_message(event.chat, text, reply_to=event.message_id)


async def process(
    event: InboundEvent,
    messaging: MessagingAdapter,
    processed: ProcessedEvents,
    services: Services | None = None,
) -> bool:
    """Handle an event exactly once. Returns False for a duplicate delivery.

    The claim comes first: if handling then fails, the event is not retried, so a bot reply or an
    action is never repeated (at-most-once after the claim, D31).
    """
    if not await processed.claim(messaging.event_source, event.event_id):
        return False
    await handle_inbound(event, messaging, services)
    # After handling: a buffer failure can't cost the command, and a request never reads itself.
    await _remember(event, services)
    return True


async def _remember(event: InboundEvent, services: Services | None) -> None:
    """Keeps plain messages from linked chats as context for later requests (D46)."""
    buffer = services.buffer if services else None
    if buffer is None or services is None or not isinstance(event, InboundMessage):
        return
    if event.sender.is_bot or not event.text or event.text.startswith("/"):
        return  # commands, known or not, are no context
    workspace = await services.workspaces.for_chat(event.chat)
    if workspace is not None and workspace.keeps_context:
        await buffer.remember(workspace.id, event)
