"""Chat commands: one handler per command name. Add a command here and in core/messages.HELP."""

from collections.abc import Mapping
from types import MappingProxyType

from chirpo.core.commands import basic, issues, workspace
from chirpo.core.commands.base import Handler, Request, Services

COMMANDS: Mapping[str, Handler] = MappingProxyType(
    {
        "ping": basic.ping,
        "help": basic.help_,
        "start": basic.start,
        "about": basic.about,
        "iam": basic.iam,
        "iam_switch": basic.iam_switch,
        "link": workspace.link,
        "unlink": workspace.unlink,
        "members": workspace.members,
        "role": workspace.role,
        "issue": issues.issue,
        "context": workspace.context,
        "personality": workspace.personality,
    }
)

__all__ = ["COMMANDS", "Handler", "Request", "Services"]
