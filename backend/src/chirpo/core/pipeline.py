"""Message -> interpret -> validate -> authorise -> confirm -> execute.

M1 handles only deterministic commands; interpretation by the LLM arrives in M4.
"""

from collections.abc import Mapping
from types import MappingProxyType

from chirpo.core import messages
from chirpo.core.domain import InboundEvent, InboundMessage
from chirpo.core.ports import MessagingAdapter
from chirpo.core.router import is_explicitly_addressed, parse_command

COMMAND_REPLIES: Mapping[str, str] = MappingProxyType(
    {
        "ping": messages.PONG,
        "help": messages.HELP,
        "start": messages.HELP,
        "about": messages.ABOUT,
    }
)


def _reply_for(message: InboundMessage) -> str | None:
    command = parse_command(message.text)
    explicit = is_explicitly_addressed(message)
    if command is not None and command.name in COMMAND_REPLIES:
        return COMMAND_REPLIES[command.name]
    if not explicit:
        # Unknown commands in a group may belong to another bot: stay quiet (D22).
        return None
    return messages.UNKNOWN_COMMAND if command is not None else messages.NOT_UNDERSTOOD_YET


async def handle_inbound(event: InboundEvent, messaging: MessagingAdapter) -> None:
    if not isinstance(event, InboundMessage) or event.sender.is_bot:
        return  # button presses are handled from M5; bots never trigger the bot (no loops)
    reply = _reply_for(event)
    if reply is not None:
        await messaging.send_message(event.chat, reply, reply_to=event.message_id)
