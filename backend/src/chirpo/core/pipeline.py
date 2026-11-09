"""Message -> interpret -> validate -> authorise -> confirm -> execute.

M1 handles only deterministic commands; interpretation by the LLM arrives in M4.
"""

from collections.abc import Mapping
from types import MappingProxyType

from chirpo.core import messages
from chirpo.core.domain import InboundEvent, InboundMessage
from chirpo.core.identity import IdentityLinking
from chirpo.core.ports import MessagingAdapter, ProcessedEvents
from chirpo.core.router import Command, is_explicitly_addressed, parse_command

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


def _is_iam(command: Command | None) -> bool:
    # "/start iam" is what Telegram sends when someone opens the bot from the /iam link.
    return command is not None and (
        command.name in ("iam", "iam_switch") or (command.name == "start" and command.args == "iam")
    )


def _is_iam_switch(command: Command) -> bool:
    # One word so chat apps make the whole command tappable; "/iam switch" typed by hand works too.
    return command.name == "iam_switch" or (
        command.name == "iam" and command.args.lower() == "switch"
    )


async def handle_inbound(
    event: InboundEvent, messaging: MessagingAdapter, identity: IdentityLinking | None = None
) -> None:
    if not isinstance(event, InboundMessage) or event.sender.is_bot:
        return  # button presses are handled from M5; bots never trigger the bot (no loops)
    command = parse_command(event.text)
    if command is not None and _is_iam(command):
        if identity is None:
            await messaging.send_message(
                event.chat, messages.GITHUB_LOGIN_UNAVAILABLE, reply_to=event.message_id
            )
        else:
            await identity.start(event, messaging, switch=_is_iam_switch(command))
        return
    reply = _reply_for(event)
    if reply is not None:
        await messaging.send_message(event.chat, reply, reply_to=event.message_id)


async def process(
    event: InboundEvent,
    messaging: MessagingAdapter,
    processed: ProcessedEvents,
    identity: IdentityLinking | None = None,
) -> bool:
    """Handle an event exactly once. Returns False for a duplicate delivery.

    The claim comes first: if handling then fails, the event is not retried, so a bot reply or an
    action is never repeated (at-most-once after the claim, D31).
    """
    if not await processed.claim(messaging.event_source, event.event_id):
        return False
    await handle_inbound(event, messaging, identity)
    return True
