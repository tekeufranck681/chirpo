"""/templates pr (M6.16): a pull request template with a `Closes #` line, proposed as a pull
request (never pushed to the default branch); an existing template is never touched."""

import pytest
from chirpo.core.commands.templates import PR_TEMPLATE_BRANCH, PR_TEMPLATE_PATH

from chirpo.core import messages
from chirpo.core.domain import AuditResult, RepoPermission
from chirpo.core.errors import CodeHostUnavailableError
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


async def test_usage(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/templates") == messages.TEMPLATES_USAGE
    assert await world.say("/templates issue") == messages.TEMPLATES_USAGE
    assert "/templates" in messages.HELP
