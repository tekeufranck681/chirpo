"""Decides whether a message is for the bot, and which command it is.

Adapters normalise text first: mentions of the bot and Telegram's "/cmd@botname" suffix are
removed (and `mentions_bot` set), and commands aimed at other bots are dropped. So the core only
sees "/name".
"""

import re
from dataclasses import dataclass

from chirpo.core.domain import InboundMessage

COMMAND_NAME = re.compile(r"/(?P<name>[A-Za-z0-9_]{1,32})")


@dataclass(frozen=True)
class Command:
    name: str
    args: str


def parse_command(text: str) -> Command | None:
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    match = COMMAND_NAME.fullmatch(parts[0])
    if match is None:
        return None
    return Command(match.group("name").lower(), parts[1].strip() if len(parts) > 1 else "")


def is_explicitly_addressed(message: InboundMessage) -> bool:
    return message.is_private_chat or message.mentions_bot or message.replies_to_bot
