"""/templates pr [repo] (M6.16, D25, D38): proposes a pull request template with a `Closes #`
line, so pull requests link the issue they fix. It is opened as a pull request from its own
branch, never pushed to the default branch, and a repo's existing template is never touched.
"""

import hashlib
import re

from chirpo.core import messages
from chirpo.core.commands.base import Request, allowed, audit, find_actor, limited
from chirpo.core.context import MAX_NAME
from chirpo.core.domain import AuditResult, FileProposal, InboundMessage, RepoRef
from chirpo.core.errors import CodeHostEmptyRepoError, CodeHostError
from chirpo.core.links import GAP, KEYWORDS
from chirpo.core.outbound import shown
from chirpo.core.permissions import Action
from chirpo.core.resolution import repos_named

PR_TEMPLATE_BRANCH = "chirpo/pr-template"
PR_TEMPLATE_PATH = ".github/pull_request_template.md"
PR_TEMPLATE = """## What changed

<!-- What this pull request does, and why. -->

Closes #

## How it was tested

<!-- Steps, screenshots, or the tests that cover it. -->
"""
PR_TEMPLATE_BODY = (
    "Adds a pull request template with a `Closes #` line: a pull request that names the issue it "
    "fixes is linked to it, and GitHub closes the issue when the pull request is merged into the "
    "default branch.\n\nEdit it as you like before merging. Requested by @{login} via chirpo."
)
PR_TEMPLATE_PASTED_BODY = (
    "Adds the pull request template pasted in chat. Edit it as you like before merging. "
    "Requested by @{login} via chirpo."
)
_CLOSING_PLACEHOLDER = re.compile(rf"\b{KEYWORDS}{GAP}#", re.IGNORECASE)


async def templates(request: Request) -> None:
    kind, _, named = request.args.strip().partition(" ")
    if kind.lower() != "pr":
        await request.reply(messages.TEMPLATES_USAGE)
        return
    actor = await find_actor(request, no_workspace=messages.NO_REPO_LINKED)
    if actor is None:
        return
    links = [link.repo for link in await actor.services.workspaces.repos(actor.workspace.id)]
    if not links:
        await request.reply(messages.NO_REPO_LINKED)
        return
    repo = links[0]  # the default comes first
    if named.strip():
        found = repos_named(named, links)
        if len(found) != 1:
            await request.reply(_which(named, found, links))
            return
        repo = found[0]
    if not await allowed(request, actor, Action.PROPOSE_TEMPLATE, repo):
        return
    content = _pasted(request.message)
    if content == "":
        await request.reply(messages.PR_TEMPLATE_NOTHING_PASTED)
        return
    login = actor.member.codehost.login if actor.member.codehost else "?"
    proposal = _proposal(login, content)
    codehost = actor.services.codehost
    payload = {"repo": repo.full_name, "branch": proposal.branch}
    if content is not None:  # whose text it was: the message stays in the chat, not the audit
        payload["pasted_from"] = request.message.reply_to_message_id or ""
    try:
        existing = await codehost.pull_request_template(repo)
        if existing is not None:
            has_line = existing == "" or _CLOSING_PLACEHOLDER.search(existing)
            text = (
                messages.PR_TEMPLATE_EXISTS if has_line else messages.PR_TEMPLATE_EXISTS_NO_CLOSES
            )
            await request.reply(text.format(repo=repo.full_name))
            return
        limits = actor.services.limits
        if limits is not None and await limited(request, await limits.check_write(actor.member.id)):
            return
        url = await codehost.propose_file(repo, proposal)
    except CodeHostError as error:
        await audit(
            actor, Action.PROPOSE_TEMPLATE, AuditResult.FAILED, payload, type(error).__name__
        )
        if isinstance(error, CodeHostEmptyRepoError):  # the team's state, not a failure
            await request.reply(messages.PR_TEMPLATE_EMPTY_REPO.format(repo=repo.full_name))
            return
        await request.reply(messages.GITHUB_TROUBLE)
        raise
    await audit(actor, Action.PROPOSE_TEMPLATE, AuditResult.EXECUTED, {**payload, "url": url})
    has_line = content is None or _CLOSING_PLACEHOLDER.search(content)
    text = messages.PR_TEMPLATE_PROPOSED if has_line else messages.PR_TEMPLATE_PROPOSED_NO_CLOSES
    await request.reply(text.format(repo=repo.full_name, url=url))


def _pasted(message: InboundMessage) -> str | None:
    """The template pasted in the message replied to, "" when it holds no text, None when the
    command isn't a reply. A reply to chirpo's own message gets chirpo's template."""
    if message.reply_to_message_id is None or message.replies_to_bot:
        return None
    return message.reply_to_text.rstrip()


def _proposal(login: str, pasted: str | None) -> FileProposal:
    if pasted is None:
        return FileProposal(
            branch=PR_TEMPLATE_BRANCH,
            path=PR_TEMPLATE_PATH,
            content=PR_TEMPLATE,
            commit_message="chore: add a pull request template",
            title="Add a pull request template",
            body=PR_TEMPLATE_BODY.format(login=login),
        )
    content = f"{pasted}\n"
    # One branch per text: a new paste gets its own PR, the same paste sent twice reuses it
    digest = hashlib.sha256(content.encode()).hexdigest()[:8]
    return FileProposal(
        branch=f"{PR_TEMPLATE_BRANCH}-{digest}",
        path=PR_TEMPLATE_PATH,
        content=content,
        commit_message="chore: add a pull request template",
        title="Add a pull request template",
        body=PR_TEMPLATE_PASTED_BODY.format(login=login),
    )


def _which(named: str, found: list[RepoRef], links: list[RepoRef]) -> str:
    name = shown(named.strip(), MAX_NAME)
    if found:
        options = ", ".join(repo.full_name for repo in found)
        return messages.REPO_AMBIGUOUS.format(name=name, options=options)
    repos = ", ".join(repo.full_name for repo in links)
    return messages.REPO_NOT_LINKED.format(name=name, repos=repos)
