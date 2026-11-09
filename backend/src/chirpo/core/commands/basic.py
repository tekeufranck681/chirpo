"""Commands that need nothing but the chat: /ping, /help, /about, and /iam's entry points."""

from chirpo.core import messages
from chirpo.core.commands.base import Request


async def ping(request: Request) -> None:
    await request.reply(messages.PONG)


async def help_(request: Request) -> None:
    await request.reply(messages.HELP.format(bot=request.messaging.bot_mention()))


async def about(request: Request) -> None:
    await request.reply(messages.ABOUT)


async def start(request: Request) -> None:
    # "/start iam" is what Telegram sends when someone opens the bot from the /iam link.
    if request.args == "iam":
        await iam(request)
    else:
        await help_(request)


async def iam(request: Request) -> None:
    await _iam(request, switch=request.args.lower() == "switch")


async def iam_switch(request: Request) -> None:
    # One word so chat apps make the whole command tappable; "/iam switch" works too.
    await _iam(request, switch=True)


async def _iam(request: Request, *, switch: bool) -> None:
    identity = request.services.identity if request.services else None
    if identity is None:
        await request.reply(messages.GITHUB_LOGIN_UNAVAILABLE)
        return
    await identity.start(request.message, request.messaging, switch=switch)
