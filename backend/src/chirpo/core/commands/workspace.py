"""/link, /unlink, /members, /role: who and which repos belong to this chat's workspace."""

from chirpo.core import messages
from chirpo.core.commands.base import Actor, Request, allowed, audit, find_actor
from chirpo.core.domain import AuditResult, RepoRef
from chirpo.core.errors import LastAdminError
from chirpo.core.permissions import Action, Role
from chirpo.core.workspaces import Member

MAX_LISTED = 50  # a chat message holds about 4,000 characters


def _repo(request: Request, provider: str) -> RepoRef | None:
    try:
        return RepoRef.parse(provider, request.args)
    except ValueError:
        return None


async def link(request: Request) -> None:
    actor = await find_actor(request)
    if actor is None:
        return
    repo = _repo(request, actor.services.codehost.provider)
    if repo is None:
        await request.reply(messages.LINK_USAGE)
        return
    linked = await actor.services.workspaces.repos(actor.workspace.id)
    if any(link.repo.key == repo.key for link in linked):
        await request.reply(messages.ALREADY_LINKED.format(repo=repo.full_name))
        return
    first = not linked
    action = Action.LINK_FIRST_REPO if first else Action.LINK_REPO
    if first and not await _runs_this_chat(request):
        await audit(actor, action, AuditResult.DENIED, {"repo": repo.full_name}, "not_group_admin")
        await request.reply(messages.FIRST_LINK_NEEDS_GROUP_ADMIN)
        return
    if not await allowed(request, actor, action, repo):
        return
    link = await actor.services.workspaces.link_repo(
        actor.workspace.id, repo, first_admin=actor.member.id if first else None
    )
    if link is None:  # someone else linked the first repo meanwhile: now admins only
        first, action = False, Action.LINK_REPO
        if not await allowed(request, actor, action, repo):
            return
        link = await actor.services.workspaces.link_repo(actor.workspace.id, repo)
    await audit(actor, action, AuditResult.EXECUTED, {"repo": repo.full_name})
    text = messages.LINKED.format(repo=repo.full_name)
    if link is not None and link.is_default:
        text += messages.LINKED_DEFAULT
    if first:
        text += messages.LINKED_ADMIN
        if actor.services.buffer is not None and actor.workspace.keeps_context:
            text += messages.CONTEXT_NOTICE.format(hours=actor.services.context_hours)
    await request.reply(text)


async def _runs_this_chat(request: Request) -> bool:
    """In a group, only its own admins may claim chirpo's admin role with the first link."""
    message = request.message
    if message.is_private_chat:
        return True
    return await request.messaging.is_chat_admin(message.chat, message.sender)


async def unlink(request: Request) -> None:
    actor = await find_actor(request)
    if actor is None:
        return
    repo = _repo(request, actor.services.codehost.provider)
    if repo is None:
        await request.reply(messages.UNLINK_USAGE)
        return
    if not await allowed(request, actor, Action.UNLINK_REPO):
        return
    if not await actor.services.workspaces.unlink_repo(actor.workspace.id, repo):
        await request.reply(messages.NOT_LINKED_HERE.format(repo=repo.full_name))
        return
    await audit(actor, Action.UNLINK_REPO, AuditResult.EXECUTED, {"repo": repo.full_name})
    await request.reply(messages.UNLINKED.format(repo=repo.full_name))


async def context(request: Request) -> None:
    """Whether the chat's recent messages are kept for the LLM (D46); admins turn it on or off."""
    choice = request.args.strip().lower()
    if choice not in ("", "on", "off"):
        await request.reply(messages.CONTEXT_USAGE)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    services, workspace = actor.services, actor.workspace
    if services.buffer is None:
        await request.reply(messages.CONTEXT_OFF_FOR_INSTANCE)
        return
    if not choice:
        on = workspace.keeps_context
        text = messages.CONTEXT_IS_ON if on else messages.CONTEXT_IS_OFF
        await request.reply(text.format(hours=services.context_hours))
        return
    if not await allowed(request, actor, Action.CHANGE_SETTINGS):
        return
    keep = choice == "on"
    await services.workspaces.set_keeps_context(workspace.id, keep=keep)
    if not keep:
        await services.buffer.forget(workspace.id)
    await audit(actor, Action.CHANGE_SETTINGS, AuditResult.EXECUTED, {"keep_context": keep})
    text = messages.CONTEXT_TURNED_ON if keep else messages.CONTEXT_TURNED_OFF
    await request.reply(text.format(hours=services.context_hours))


async def members(request: Request) -> None:
    actor = await find_actor(request)
    if actor is None or not await allowed(request, actor, Action.LIST_MEMBERS):
        return
    people = await actor.services.members.all(actor.workspace.id)
    lines = [messages.MEMBERS_TITLE, *(_member_line(member) for member in people[:MAX_LISTED])]
    if len(people) > MAX_LISTED:
        lines.append(messages.MEMBERS_MORE.format(count=len(people) - MAX_LISTED))
    await request.reply("\n".join(lines))


def _member_line(member: Member) -> str:
    return messages.MEMBER_LINE.format(
        name=member.display_name,
        chat=f" (@{member.chat_username})" if member.chat_username else "",
        role=member.role,
        github=(
            messages.MEMBER_GITHUB.format(login=member.codehost.login)
            if member.codehost
            else messages.MEMBER_NOT_VERIFIED
        ),
    )


async def role(request: Request) -> None:
    parts = request.args.split()
    new_role = _role(parts[1]) if len(parts) == 2 else None
    if new_role is None:
        await request.reply(messages.ROLE_USAGE)
        return
    actor = await find_actor(request)
    if actor is None or not await allowed(request, actor, Action.CHANGE_ROLE):
        return
    name = parts[0].removeprefix("@")
    matches = await _find_members(actor, name)
    if len(matches) != 1:
        text = messages.MEMBER_AMBIGUOUS if matches else messages.MEMBER_NOT_FOUND
        await request.reply(text.format(name=f"@{name}"))
        return
    (target,) = matches
    payload = {"member": str(target.id), "role": new_role.value}
    try:
        await actor.services.members.set_role(actor.workspace.id, target.id, new_role)
    except LastAdminError:
        await audit(actor, Action.CHANGE_ROLE, AuditResult.DENIED, payload, "last_admin")
        await request.reply(messages.LAST_ADMIN)
        return
    await audit(actor, Action.CHANGE_ROLE, AuditResult.EXECUTED, payload)
    await request.reply(messages.ROLE_CHANGED.format(name=target.display_name, role=new_role))


def _role(text: str) -> Role | None:
    try:
        return Role(text.lower())
    except ValueError:
        return None


async def _find_members(actor: Actor, name: str) -> list[Member]:
    """Everyone whose chat @username or code host login is `name` (ignoring case). Chat usernames
    are chosen freely, so one could copy someone's GitHub login: callers refuse several matches."""
    wanted = name.casefold()
    return [
        member
        for member in await actor.services.members.all(actor.workspace.id)
        if any(
            handle and handle.casefold() == wanted
            for handle in (member.chat_username, member.codehost.login if member.codehost else None)
        )
    ]
