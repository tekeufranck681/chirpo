"""/templates pr (M6.16): a pull request template with a `Closes #` line, proposed as a pull
request (never pushed to the default branch); an existing template is never touched."""

import pytest

from chirpo.core import messages
from chirpo.core.commands.templates import PR_TEMPLATE_BRANCH, PR_TEMPLATE_PATH
from chirpo.core.domain import AuditResult, FileProposal, RepoPermission
from chirpo.core.errors import CodeHostUnavailableError
from chirpo.core.pipeline import handle_inbound
from tests.fakes import make_message
from tests.unit.core.test_commands import (
    FRANCK,
    FRANCK_GITHUB,
    OTHER,
    SANDBOX,
    World,
    linked_by_annie,
)


@pytest.fixture
def world() -> World:
    return World()


async def test_a_repo_without_a_template_gets_one_proposed(world: World) -> None:
    await linked_by_annie(world)
    reply = await world.say("/templates pr")
    proposal = world.codehost.proposals[(SANDBOX.key, PR_TEMPLATE_BRANCH)]
    assert proposal.path == PR_TEMPLATE_PATH
    assert "Closes #" in proposal.content
    assert "@annie-gh" in proposal.body  # who asked, as on issues
    url = f"https://fake.example/{SANDBOX.full_name}/pull/{PR_TEMPLATE_BRANCH}"
    assert reply == messages.PR_TEMPLATE_PROPOSED.format(repo=SANDBOX.full_name, url=url)
    entry = world.audit.entries[-1]
    assert (entry.action, entry.result) == ("propose_template", AuditResult.EXECUTED)


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("## What\n\nCloses #", messages.PR_TEMPLATE_EXISTS),
        ("## What\n", messages.PR_TEMPLATE_EXISTS_NO_CLOSES),
        ("", messages.PR_TEMPLATE_EXISTS),  # a folder of templates: not read
    ],
)
async def test_an_existing_template_is_never_touched(
    world: World, existing: str, expected: str
) -> None:
    await linked_by_annie(world)
    world.codehost.pr_templates[SANDBOX.key] = existing
    assert await world.say("/templates pr") == expected.format(repo=SANDBOX.full_name)
    assert world.codehost.proposals == {}


async def test_another_linked_repo_can_be_named(world: World) -> None:
    await linked_by_annie(world)
    world.github_access("annie-gh", OTHER, RepoPermission.ADMIN)
    await world.say("/link tekeufranck681/chirpo")
    await world.say("/templates pr chirpo")
    assert (OTHER.key, PR_TEMPLATE_BRANCH) in world.codehost.proposals


async def test_a_repo_that_isnt_linked_is_said(world: World) -> None:
    await linked_by_annie(world)
    reply = await world.say("/templates pr mobile")
    assert reply == messages.REPO_NOT_LINKED.format(name="mobile", repos=SANDBOX.full_name)
    assert world.codehost.proposals == {}


async def test_only_admins_with_write_access_propose_templates(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    reply = await world.say("/templates pr", FRANCK)
    assert reply == messages.ROLE_TOO_LOW.format(role="admin")
    assert world.codehost.proposals == {}


async def test_github_trouble_is_said_and_audited(world: World) -> None:
    await linked_by_annie(world)

    async def down(*_: object) -> str:
        raise CodeHostUnavailableError("down")

    world.codehost.propose_file = down  # type: ignore[method-assign]
    with pytest.raises(CodeHostUnavailableError):
        await world.say("/templates pr")
    assert world.messaging.sent[-1][1] == messages.GITHUB_TROUBLE
    assert world.audit.entries[-1].result == AuditResult.FAILED


TEMPLATE = "## Summary\n\nCloses #\n\n## Checklist\n- [ ] Tests"


async def paste(world: World, text: str, *, from_bot: bool = False) -> str:
    """`/templates pr` sent as a reply to a message holding `text`."""
    world.messaging.sent.clear()
    message = make_message(
        "/templates pr", reply_to_message_id="9", reply_to_text=text, replies_to_bot=from_bot
    )
    await handle_inbound(message, world.messaging, world.services)
    return world.messaging.sent[-1][1]


def pasted(world: World) -> list[FileProposal]:
    return [
        p for (_, branch), p in world.codehost.proposals.items() if branch != PR_TEMPLATE_BRANCH
    ]


async def test_a_pasted_template_is_proposed_as_it_is(world: World) -> None:
    await linked_by_annie(world)
    reply = await paste(world, TEMPLATE + "  \n")
    [proposal] = pasted(world)
    assert proposal.content == TEMPLATE + "\n"
    assert proposal.path == PR_TEMPLATE_PATH
    assert proposal.branch.startswith(f"{PR_TEMPLATE_BRANCH}-")
    assert "@annie-gh" in proposal.body
    url = f"https://fake.example/{SANDBOX.full_name}/pull/{proposal.branch}"
    assert reply == messages.PR_TEMPLATE_PROPOSED.format(repo=SANDBOX.full_name, url=url)


async def test_each_pasted_text_gets_its_own_proposal(world: World) -> None:
    await linked_by_annie(world)
    await paste(world, TEMPLATE)
    await paste(world, TEMPLATE)  # sent twice: the same proposal
    assert len(pasted(world)) == 1
    await paste(world, TEMPLATE + "\n- [ ] Docs")
    assert len(pasted(world)) == 2


async def test_a_pasted_template_without_closes_is_proposed_with_a_tip(world: World) -> None:
    await linked_by_annie(world)
    reply = await paste(world, "## Summary\n")
    [proposal] = pasted(world)
    url = f"https://fake.example/{SANDBOX.full_name}/pull/{proposal.branch}"
    assert reply == messages.PR_TEMPLATE_PROPOSED_NO_CLOSES.format(repo=SANDBOX.full_name, url=url)


async def test_a_reply_to_a_message_without_text_is_explained(world: World) -> None:
    await linked_by_annie(world)
    assert await paste(world, "  ") == messages.PR_TEMPLATE_NOTHING_PASTED
    assert world.codehost.proposals == {}


async def test_a_reply_to_the_bot_proposes_the_usual_template(world: World) -> None:
    await linked_by_annie(world)
    await paste(world, "Created issue #87", from_bot=True)
    assert list(world.codehost.proposals) == [(SANDBOX.key, PR_TEMPLATE_BRANCH)]


async def test_a_pasted_template_never_replaces_an_existing_one(world: World) -> None:
    await linked_by_annie(world)
    world.codehost.pr_templates[SANDBOX.key] = "Closes #"
    assert await paste(world, TEMPLATE) == messages.PR_TEMPLATE_EXISTS.format(
        repo=SANDBOX.full_name
    )
    assert world.codehost.proposals == {}


async def test_usage(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/templates") == messages.TEMPLATES_USAGE
    assert await world.say("/templates issue") == messages.TEMPLATES_USAGE
    assert "/templates" in messages.HELP


async def test_the_audit_names_the_branch_and_the_pasted_message(world: World) -> None:
    await linked_by_annie(world)
    await paste(world, TEMPLATE)
    [proposal] = pasted(world)
    entry = world.audit.entries[-1]
    assert entry.payload["branch"] == proposal.branch
    assert entry.payload["pasted_from"] == "9"
    assert TEMPLATE not in str(entry.payload)  # the text itself stays out of the audit


async def test_permission_is_checked_before_the_pasted_text(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    world.messaging.sent.clear()
    message = make_message(
        "/templates pr", sender=FRANCK, reply_to_message_id="9", reply_to_text=""
    )
    await handle_inbound(message, world.messaging, world.services)
    assert world.messaging.sent[-1][1] == messages.ROLE_TOO_LOW.format(role="admin")


async def test_an_empty_repo_is_explained_not_blamed_on_github(world: World) -> None:
    from chirpo.core.errors import CodeHostEmptyRepoError

    await linked_by_annie(world)

    async def empty(*_: object) -> str:
        raise CodeHostEmptyRepoError("empty")

    world.codehost.propose_file = empty  # type: ignore[method-assign]
    reply = await world.say("/templates pr")  # no exception: nothing is broken
    assert reply == messages.PR_TEMPLATE_EMPTY_REPO.format(repo=SANDBOX.full_name)
    entry = world.audit.entries[-1]
    assert (entry.result, entry.error) == (AuditResult.FAILED, "CodeHostEmptyRepoError")
