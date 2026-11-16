"""/catchup [3h] (M6.9): what did I miss, without the model reading the request first."""

import re

from chirpo.core import catchup as core  # the module: it imports commands.base in turn
from chirpo.core import messages
from chirpo.core.commands.base import Request, find_actor, limited, needs_services, typing_shown

_HOURS = re.compile(r"(\d{1,3})\s*h?", re.IGNORECASE)
MAX_HOURS = 720


async def catchup(request: Request) -> None:
    said = request.args.strip()
    hours = None
    if said:
        match = _HOURS.fullmatch(said)
        if match is None or not 1 <= int(match.group(1)) <= MAX_HOURS:
            await request.reply(messages.CATCHUP_USAGE)
            return
        hours = int(match.group(1))
    services = await needs_services(request)
    if services is None:
        return
    if services.llm is None:
        await request.reply(messages.LLM_NOT_CONFIGURED)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    if not actor.member.is_verified:  # the issue lines need their GitHub access
        await request.reply(messages.LINK_GITHUB_FIRST)
        return
    if services.limits is not None:
        verdict = await services.limits.check_llm(actor.workspace.id, actor.member.id)
        if await limited(request, verdict):
            return
    repos = [link.repo for link in await services.workspaces.repos(actor.workspace.id)]
    async with typing_shown(request):
        await core.catch_up(request, actor, services.llm, repos, hours=hours)
