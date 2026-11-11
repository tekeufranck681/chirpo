"""/issue <title>: create an issue in the chat's default repo, noting who asked."""

from pydantic import ValidationError

from chirpo.core import messages
from chirpo.core.commands.base import Request, allowed, audit, find_actor, limited
from chirpo.core.domain import AuditResult, NewIssue
from chirpo.core.errors import CodeHostError
from chirpo.core.permissions import Action


async def issue(request: Request) -> None:
    title = " ".join(request.args.split())  # one line: chat text can't add structure to the issue
    if not title:
        await request.reply(messages.ISSUE_USAGE)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    linked = await actor.services.workspaces.repos(actor.workspace.id)
    if not linked:
        await request.reply(messages.NO_REPO_LINKED)
        return
    repo = linked[0].repo  # the default comes first
    if not await allowed(request, actor, Action.CREATE_ISSUE, repo):
        return
    login = actor.member.codehost.login if actor.member.codehost else "?"
    try:
        new = NewIssue(
            title=title,
            body=messages.ISSUE_BODY.format(login=login),
        )
    except ValidationError:
        await request.reply(messages.ISSUE_TITLE_TOO_LONG)
        return
    payload = {"repo": repo.full_name, "title": new.title}
    limits = actor.services.limits
    if limits is not None:
        verdict = await limits.check_write(actor.member.id)
        if await limited(request, verdict):
            return
    try:
        created = await actor.services.codehost.create_issue(repo, new)
    except CodeHostError as exc:
        await audit(actor, Action.CREATE_ISSUE, AuditResult.FAILED, payload, type(exc).__name__)
        await request.reply(messages.ISSUE_FAILED.format(reason=type(exc).__name__))
        raise
    await audit(
        actor, Action.CREATE_ISSUE, AuditResult.EXECUTED, {**payload, "number": created.number}
    )
    await request.reply(
        messages.ISSUE_CREATED.format(number=created.number, title=created.title, url=created.url)
    )
