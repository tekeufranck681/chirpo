"""Linking a pull request to an issue from chat (M6.15): a closing line added to, or removed
from, the PR's description, after a card; the author's text is kept."""

import pytest

from chirpo.core import messages
from chirpo.core.actions import IssueRef, LinkPullRequest, UnlinkPullRequest
from chirpo.core.domain import NewIssue, RepoPermission, RepoRef
from chirpo.core.links import is_linked, with_link, without_link
from chirpo.core.pipeline import handle_inbound
from tests.fakes import CHAT, make_callback
from tests.unit.core.test_direct import rows
from tests.unit.core.test_interpreter import FRANCK, SANDBOX, World, _issues

WEB = RepoRef.parse("github", "tekeufranck681/chirpo-web")

# ---------------------------------------------------------------------------- the text


def test_a_closing_line_is_added_after_the_authors_text() -> None:
    assert with_link("Adds dark mode.\n", "#2") == "Adds dark mode.\n\nCloses #2"
    assert with_link("", "#2") == "Closes #2"


@pytest.mark.parametrize("body", ["Fixes #2", "closes: #2", "RESOLVED #2", "- Closes #2."])
def test_any_closing_keyword_counts_as_linked(body: str) -> None:
    assert is_linked(body, "#2")
    assert with_link(body, "#2") == body


def test_another_number_is_not_this_issue() -> None:
    assert not is_linked("Closes #20", "#2")
    assert not is_linked("Closes acme/web#2", "#2")


def test_unlinking_removes_a_line_or_only_the_keyword() -> None:
    assert without_link("Adds dark mode.\n\nCloses #2", "#2") == "Adds dark mode."
    assert without_link("Fixes #2 and tidies the CSS", "#2") == "#2 and tidies the CSS"
    assert without_link("Closes #2\nCloses #3", "#2") == "Closes #3"


def test_windows_line_ends_from_githubs_editor_are_handled() -> None:
    body = "Adds dark mode.\r\n\r\nCloses #2\r\n"
    assert without_link(body, "#2") == "Adds dark mode."
    assert with_link("Adds dark mode.\r\n", "#2") == "Adds dark mode.\n\nCloses #2"


def test_unlinking_leaves_the_rest_of_the_text_alone() -> None:
    body = "Intro\n\n```\na\n\n\n\nb\n```\n\nCloses #2\n\nThanks"
    assert without_link(body, "#2") == "Intro\n\n```\na\n\n\n\nb\n```\n\nThanks"
    assert without_link("Closes #2\n\nText", "#2") == "Text"


def test_a_description_full_of_spaces_is_checked_quickly() -> None:
    import time

    body = "close" + " " * 65_000 + "x"
    start = time.perf_counter()
    assert not is_linked(body, "#2", "acme/web#2")
    assert without_link(body, "#2") == body
    assert time.perf_counter() - start < 0.5


def test_a_templates_empty_closes_line_is_filled_in() -> None:
    template = "## What changed\n\nAdds a toggle.\n\nCloses #\n\n## How it was tested\n"
    assert with_link(template, "#8") == (
        "## What changed\n\nAdds a toggle.\n\nCloses #8\n\n## How it was tested\n"
    )
    assert with_link("- Fixes: #", "#8") == "- Fixes: #8"
    assert with_link("Closes #3", "#8") == "Closes #3\n\nCloses #8"  # a real link stays


# ---------------------------------------------------------------------------- from chat


async def team(*answers: object, body: str = "", closes: tuple[int, ...] = ()) -> World:
    """Issues #1 to #3 in the sandbox and PR #4 "Dark mode toggle"."""
    world = World(*answers)  # type: ignore[arg-type]
    await world.linked()
    await _issues(world)
    world.codehost.add_pull_request(
        SANDBOX, title="Dark mode toggle", body=body, closes_issues=closes
    )
    return world


async def confirm(world: World) -> str:
    [row] = [r for r in rows(world) if r.message_id]
    world.messaging.edited.clear()
    callback = make_callback(f"ok:{row.id}", sender=FRANCK, message_id=row.message_id or "")
    await handle_inbound(callback, world.messaging, world.services)
    return world.messaging.edited[-1][1]


def description(world: World, repo: RepoRef = SANDBOX, number: int = 4) -> str:
    return world.codehost.pulls[(repo.key, number)].body


async def test_linking_shows_a_card_then_adds_the_closing_line() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Adds a toggle."
    )
    card = await world.say("link !4 to #2")
    assert card.startswith(messages.DRY_RUN_HEADER)
    assert '"Dark mode toggle"' in card and '#2 "Add dark mode"' in card
    assert description(world) == "Adds a toggle."  # nothing before Confirm
    result = await confirm(world)
    assert description(world) == "Adds a toggle.\n\nCloses #2"
    assert (
        messages.DONE_LINKED.format(pull=4, repo=SANDBOX.full_name, issue='#2 "Add dark mode"')
        in result
    )


async def test_an_existing_link_is_left_as_it_is() -> None:
    world = await team([LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Fixes #2")
    await world.say("link !4 to #2")
    result = await confirm(world)
    assert description(world) == "Fixes #2"
    assert (
        messages.DONE_ALREADY_LINKED.format(
            pull=4, repo=SANDBOX.full_name, issue='#2 "Add dark mode"'
        )
        in result
    )


async def test_unlinking_keeps_the_rest_of_the_description() -> None:
    world = await team(
        [UnlinkPullRequest(pull_request=4, issue=IssueRef(number=2))],
        body="Adds a toggle.\n\nCloses #2",
    )
    await world.say("unlink !4 from #2")
    result = await confirm(world)
    assert description(world) == "Adds a toggle."
    assert (
        messages.DONE_UNLINKED.format(pull=4, repo=SANDBOX.full_name, issue='#2 "Add dark mode"')
        in result
    )


async def test_a_link_made_in_githubs_sidebar_is_explained() -> None:
    world = await team(
        [UnlinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="", closes=(2,)
    )
    await world.say("unlink !4 from #2")
    result = await confirm(world)
    assert (
        messages.DONE_LINKED_IN_SIDEBAR.format(
            pull=4, repo=SANDBOX.full_name, issue='#2 "Add dark mode"'
        )
        in result
    )


async def test_an_issue_in_another_linked_repo_gets_its_full_name() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=1), issue_repo="web")]
    )
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.workspaces.link_repo(workspace.id, WEB)
    world.codehost.permissions[(WEB.key, "tekeufranck681")] = RepoPermission.WRITE
    await world.codehost.create_issue(WEB, NewIssue(title="Footer links broken"))
    card = await world.say("link !4 to #1 in web")
    assert WEB.full_name in card
    await confirm(world)
    assert description(world) == f"Closes {WEB.full_name}#1"


async def test_someone_saving_in_between_keeps_their_text() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Adds a toggle."
    )
    real = world.codehost.update_pull_request_body
    saved = False

    async def racing(repo: RepoRef, number: int, body: str) -> None:
        nonlocal saved
        await real(repo, number, body)
        if not saved:  # the author saves right after chirpo's first write, over it
            saved = True
            await real(repo, number, "Adds a toggle and a setting.")

    world.codehost.update_pull_request_body = racing  # type: ignore[method-assign]
    await world.say("link !4 to #2")
    await confirm(world)
    assert description(world) == "Adds a toggle and a setting.\n\nCloses #2"


async def test_a_description_that_keeps_changing_stops_with_a_reason() -> None:
    world = await team([LinkPullRequest(pull_request=4, issue=IssueRef(number=2))])
    real = world.codehost.update_pull_request_body

    async def overwritten(repo: RepoRef, number: int, body: str) -> None:
        await real(repo, number, body)
        await real(repo, number, "someone else's text")

    world.codehost.update_pull_request_body = overwritten  # type: ignore[method-assign]
    await world.say("link !4 to #2")
    result = await confirm(world)
    assert messages.PR_KEPT_CHANGING.format(pull=4) in result


async def test_an_unknown_pull_request_brings_a_question() -> None:
    world = await team([LinkPullRequest(pull_request=99, issue=IssueRef(number=2))])
    text = await world.say("link !99 to #2")
    assert text == messages.PULL_REQUEST_NOT_FOUND.format(number=99, repo=SANDBOX.full_name)


async def test_linking_needs_write_access_on_github() -> None:
    world = await team([LinkPullRequest(pull_request=4, issue=IssueRef(number=2))])
    world.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.TRIAGE
    text = await world.say("link !4 to #2")
    assert "write" in text
    assert description(world) == ""


async def test_an_issue_in_another_repo_needs_the_right_to_close_it_there() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=1), issue_repo="web")]
    )
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.workspaces.link_repo(workspace.id, WEB)
    world.codehost.permissions[(WEB.key, "tekeufranck681")] = RepoPermission.READ
    await world.codehost.create_issue(WEB, NewIssue(title="Footer links broken"))
    await world.say("link !4 to #1 in web")
    result = await confirm(world)
    assert description(world) == ""  # merging would have closed an issue they can't close
    assert messages.STEP_NO_LONGER_ALLOWED.format(name="Tekeu Franck") in result


async def test_a_merged_pull_request_is_not_linked() -> None:
    from chirpo.core.domain import PullRequestState

    world = await team([LinkPullRequest(pull_request=4, issue=IssueRef(number=2))])
    key = (SANDBOX.key, 4)
    world.codehost.pulls[key] = world.codehost.pulls[key].model_copy(
        update={"state": PullRequestState.MERGED}
    )
    text = await world.say("link !4 to #2")
    assert text == messages.PULL_REQUEST_NOT_OPEN.format(number=4, state="merged")


async def test_a_stale_read_after_writing_still_reports_the_link() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Adds a toggle."
    )
    real_get = world.codehost.get_pull_request
    reads = 0

    async def stale(repo: RepoRef, number: int) -> object:
        nonlocal reads
        reads += 1
        found = await real_get(repo, number)
        if reads == 4:  # the read right after our write still shows the old text
            return found.model_copy(update={"body": "Adds a toggle."})
        return found

    world.codehost.get_pull_request = stale  # type: ignore[method-assign,assignment]
    await world.say("link !4 to #2")
    result = await confirm(world)
    assert (
        messages.DONE_LINKED.format(pull=4, repo=SANDBOX.full_name, issue='#2 "Add dark mode"')
        in result
    )


def into_staging(world: World) -> None:
    key = (SANDBOX.key, 4)
    world.codehost.pulls[key] = world.codehost.pulls[key].model_copy(
        update={"base_branch": "staging", "into_default": False}
    )


async def test_a_pull_request_into_another_branch_is_linked_with_a_warning() -> None:
    world = await team(
        [LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Adds a toggle."
    )
    into_staging(world)
    card = await world.say("link !4 to #2")
    assert messages.PULL_REQUEST_NOT_INTO_DEFAULT.format(number=4, base="staging") in card
    result = await confirm(world)
    assert description(world) == "Adds a toggle.\n\nCloses #2"
    names = {"pull": 4, "repo": SANDBOX.full_name, "issue": '#2 "Add dark mode"'}
    assert messages.DONE_LINKED_ELSEWHERE.format(**names, base="staging") in result


async def test_an_existing_link_into_another_branch_still_warns() -> None:
    world = await team([LinkPullRequest(pull_request=4, issue=IssueRef(number=2))], body="Fixes #2")
    into_staging(world)
    await world.say("link !4 to #2")
    result = await confirm(world)
    names = {"pull": 4, "repo": SANDBOX.full_name, "issue": '#2 "Add dark mode"'}
    assert messages.DONE_ALREADY_LINKED_ELSEWHERE.format(**names, base="staging") in result


async def test_unlinking_from_another_branch_needs_no_warning() -> None:
    world = await team(
        [UnlinkPullRequest(pull_request=4, issue=IssueRef(number=2))],
        body="Adds a toggle.\n\nCloses #2",
    )
    into_staging(world)
    card = await world.say("unlink !4 from #2")
    assert "staging" not in card
    await confirm(world)
    assert description(world) == "Adds a toggle."
