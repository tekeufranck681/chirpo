"""/settings (M6.4, M6.5): the default repo, the team's status labels, the issue language, the
chat's daily summary and the workspace's timezone.

Anyone can see them; admins change them, one at a time, and each change is audited. The values
are checked here, before anything is stored: a label name or a language can't smuggle in text.
"""

from typing import Any

from chirpo.core import messages
from chirpo.core.commands.base import Actor, Request, allowed, audit, find_actor
from chirpo.core.context import MAX_NAME, default_of
from chirpo.core.domain import AuditResult, RepoRef
from chirpo.core.errors import CodeHostError, CodeHostForbiddenError
from chirpo.core.languages import LANGUAGES, language_code
from chirpo.core.outbound import shown
from chirpo.core.permissions import Action
from chirpo.core.statuses import STATUSES, StatusLabels, label_problem, status_named
from chirpo.core.summary import parse_time, timezone_named
from chirpo.core.workspaces import SummarySchedule


async def settings(request: Request) -> None:
    key, rest = (*request.args.split(maxsplit=1), "", "")[:2]
    change = {
        "repo": _repo,
        "status": _status,
        "language": _language,
        "summary": _summary,
        "timezone": _timezone,
    }.get(key.lower())
    if key and change is None:
        await request.reply(messages.SETTINGS_USAGE)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    if change is None:
        await request.reply(await _overview(request, actor))
        return
    if not await allowed(request, actor, Action.CHANGE_SETTINGS):
        return
    await change(request, actor, rest.strip())


async def _overview(request: Request, actor: Actor) -> str:
    services, workspace = actor.services, actor.workspace
    schedule = await services.workspaces.summary_schedule(request.message.chat)
    links = await services.workspaces.repos(workspace.id)
    labels = workspace.statuses
    statuses = ", ".join(
        messages.SETTINGS_STATUS.format(status=s, label=shown(labels.label(s), MAX_NAME))
        for s in STATUSES
    )
    if services.buffer is None:
        context = messages.SETTINGS_CONTEXT_INSTANCE_OFF
    else:
        context = messages.SETTINGS_CONTEXT[workspace.keeps_context]
    return messages.SETTINGS.format(
        repo=default_of(links).full_name if links else messages.SETTINGS_NO_REPO,
        statuses=statuses,
        language=LANGUAGES.get(workspace.issue_language or "", messages.SETTINGS_LANGUAGE_MESSAGE),
        personality=workspace.personality,
        context=context,
        summary=_schedule_text(schedule) if schedule else messages.SUMMARY_IS_OFF,
        timezone=workspace.timezone,
    )


async def _repo(request: Request, actor: Actor, said: str) -> None:
    try:
        repo = RepoRef.parse(actor.services.codehost.provider, said)
    except ValueError:
        await request.reply(messages.SETTINGS_USAGE)
        return
    workspaces = actor.services.workspaces
    links = await workspaces.repos(actor.workspace.id)
    link = next((link for link in links if link.repo.key == repo.key), None)
    if link is None:
        await request.reply(messages.NOT_LINKED_HERE.format(repo=shown(repo.full_name, MAX_NAME)))
        return
    if link.is_default:
        await request.reply(messages.DEFAULT_REPO_ALREADY.format(repo=link.repo.full_name))
        return
    if not await workspaces.set_default_repo(actor.workspace.id, link.repo):  # unlinked meanwhile
        await request.reply(messages.NOT_LINKED_HERE.format(repo=link.repo.full_name))
        return
    was = next((other.repo.full_name for other in links if other.is_default), None)
    await _done(actor, {"default_repo": link.repo.full_name, "was": was})
    await request.reply(messages.DEFAULT_REPO_SET.format(repo=link.repo.full_name))


async def _status(request: Request, actor: Actor, said: str) -> None:
    current = actor.workspace.statuses
    if said.lower() == "reset":
        await _relabel(request, actor, StatusLabels(), messages.STATUS_LABELS_RESET, "reset")
        return
    word, equals, name = said.partition("=")
    if not equals or not word.strip():
        await request.reply(messages.SETTINGS_USAGE)
        return
    status = status_named(word, current)
    if status is None:
        await request.reply(messages.STATUS_UNKNOWN.format(status=shown(word.strip(), MAX_NAME)))
        return
    name = name.strip()
    if problem := label_problem(name, current, status):
        await request.reply(problem)
        return
    done = messages.STATUS_LABEL_SET.format(status=status, label=shown(name, MAX_NAME))
    await _relabel(request, actor, current.with_label(status, name), done, {status: name})


async def _relabel(
    request: Request, actor: Actor, wanted: StatusLabels, done: str, change: object
) -> None:
    """Saves the labels, then renames the old ones on GitHub so issues show the new names. The
    change is audited even if the renaming breaks halfway, with what happened in each repo."""
    current = actor.workspace.statuses
    await actor.services.workspaces.set_status_labels(actor.workspace.id, wanted)
    renames = [
        (current.label(s), wanted.label(s)) for s in STATUSES if current.label(s) != wanted.label(s)
    ]
    outcomes: dict[str, dict[str, str]] = {}
    try:
        lines = await _rename_on_github(actor, renames, outcomes)
    finally:
        payload: dict[str, Any] = {"status_labels": change, "was": _labels(current)}
        if outcomes:
            payload["github"] = outcomes
        await _done(actor, payload)
    await request.reply("\n".join([done, *lines]))


async def _rename_on_github(
    actor: Actor, renames: list[tuple[str, str]], outcomes: dict[str, dict[str, str]]
) -> list[str]:
    """Renames each old label in every linked repo that has it (GitHub then shows the new name on
    every issue), as far as the admin may edit labels there. Returns the lines to tell; fills
    `outcomes` with {repo: {old label: renamed | exists | no_access | failed}}."""
    lines: list[str] = []
    if not renames:
        return lines
    codehost = actor.services.codehost
    for link in await actor.services.workspaces.repos(actor.workspace.id):
        repo, name = link.repo, link.repo.full_name
        outcome = outcomes.setdefault(name, {})
        try:
            names = {label.casefold(): label for label in await codehost.list_labels(repo)}
        except CodeHostError:
            names = None
        if names is None:
            outcome.update({old: "failed" for old, _ in renames})
            lines += [_line(messages.LABEL_NOT_RENAMED, name, old) for old, _ in renames]
            continue
        found = [(names[old.casefold()], new) for old, new in renames if old.casefold() in names]
        refusal = await _edit_refusal(actor, repo) if found else None
        if refusal is not None:
            text = (
                messages.LABEL_NO_ACCESS if refusal == "no_access" else messages.LABEL_NOT_RENAMED
            )
            outcome.update({old: refusal for old, _ in found})
            lines += [_line(text, name, old) for old, _ in found]
            continue
        for old, new in found:
            if new.casefold() in names and new.casefold() != old.casefold():
                outcome[old] = "exists"
                lines.append(_line(messages.LABEL_BOTH, name, old, new))
                continue
            try:
                await codehost.rename_label(repo, old, new)
            except CodeHostError:
                outcome[old] = "failed"
                lines.append(_line(messages.LABEL_NOT_RENAMED, name, old))
                continue
            del names[old.casefold()]
            names[new.casefold()] = new
            outcome[old] = "renamed"
            lines.append(_line(messages.LABEL_RENAMED, name, old, new))
        if not outcome:
            del outcomes[name]
    return lines


async def _edit_refusal(actor: Actor, repo: RepoRef) -> str | None:
    """None if the admin may rename labels in `repo`; else why not ("no_access" or "failed")."""
    try:
        verdict = await actor.services.permissions.check(actor.member, Action.EDIT_LABELS, repo)
    except CodeHostForbiddenError:  # the App isn't installed there
        return "no_access"
    except CodeHostError:  # GitHub trouble: couldn't tell
        return "failed"
    return None if verdict.allowed else "no_access"


def _line(template: str, repo: str, old: str, new: str = "") -> str:
    return template.format(
        repo=repo, old=shown(old, MAX_NAME), new=shown(new, MAX_NAME) if new else ""
    )


async def _language(request: Request, actor: Actor, said: str) -> None:
    off = said.lower() == "off"
    code = None if off else language_code(said)
    if not off and code is None:
        names = ", ".join(sorted(LANGUAGES.values()))
        await request.reply(messages.LANGUAGE_UNKNOWN.format(names=names))
        return
    await actor.services.workspaces.set_issue_language(actor.workspace.id, code)
    await _done(actor, {"issue_language": code, "was": actor.workspace.issue_language})
    if code is None:
        await request.reply(messages.LANGUAGE_OFF)
    else:
        await request.reply(messages.LANGUAGE_SET.format(language=LANGUAGES[code]))


async def _summary(request: Request, actor: Actor, said: str) -> None:
    chat = request.message.chat
    words = said.lower().split()
    was = await actor.services.workspaces.summary_schedule(chat)
    if words == ["off"]:
        await actor.services.workspaces.set_summary_schedule(chat, None)
        await _done(actor, {"summary": "off", "was": _audited(was)})
        await request.reply(messages.SUMMARY_OFF)
        return
    at = parse_time(words[0]) if words else None
    if at is None or words[1:] not in ([], ["weekdays"]):
        await request.reply(messages.SUMMARY_USAGE)
        return
    schedule = SummarySchedule(at=at, weekdays_only=words[1:] == ["weekdays"])
    await actor.services.workspaces.set_summary_schedule(chat, schedule)
    await _done(actor, {"summary": _audited(schedule), "was": _audited(was)})
    await request.reply(
        messages.SUMMARY_SET.format(
            at=at.strftime("%H:%M"), days=_days(schedule), timezone=actor.workspace.timezone
        )
    )


async def _timezone(request: Request, actor: Actor, said: str) -> None:
    name = timezone_named(said)
    if name is None:
        await request.reply(messages.TIMEZONE_UNKNOWN)
        return
    await actor.services.workspaces.set_timezone(actor.workspace.id, name)
    await _done(actor, {"timezone": name, "was": actor.workspace.timezone})
    await request.reply(messages.TIMEZONE_SET.format(timezone=name))


def _schedule_text(schedule: SummarySchedule) -> str:
    return messages.SUMMARY_AT.format(at=schedule.at.strftime("%H:%M"), days=_days(schedule))


def _days(schedule: SummarySchedule) -> str:
    return messages.SUMMARY_WEEKDAYS if schedule.weekdays_only else ""


def _audited(schedule: SummarySchedule | None) -> str | None:
    if schedule is None:
        return None
    return schedule.at.strftime("%H:%M") + (" weekdays" if schedule.weekdays_only else "")


def _labels(labels: StatusLabels) -> dict[str, str]:
    return {status: labels.label(status) for status in STATUSES}


async def _done(actor: Actor, payload: dict[str, Any]) -> None:
    await audit(actor, Action.CHANGE_SETTINGS, AuditResult.EXECUTED, payload)
