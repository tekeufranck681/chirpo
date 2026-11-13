"""Sprints as GitHub milestones (M6.3): "what's open this sprint?" lists the issues of the current
milestone; a named milestone works too; anything unclear becomes a question, never a guess."""

from datetime import date

import pytest

from chirpo.core import messages
from chirpo.core.actions import ListIssues, MyIssues
from chirpo.core.domain import Milestone
from chirpo.core.errors import CodeHostUnavailableError
from tests.unit.core.test_interpreter import SANDBOX, World, _issues

SPRINT_11 = Milestone(number=1, title="Sprint 11", due_on=date(2026, 11, 1))
SPRINT_12 = Milestone(number=2, title="Sprint 12", due_on=date(2026, 11, 20))
V2 = Milestone(number=3, title="v2.0")


async def team(*milestones: Milestone, in_milestone: dict[int, int] | None = None) -> World:
    """#1 checkout crash (Franck), #2 dark mode (Annie), #3 CSV export (both); `in_milestone`
    puts issue numbers into milestone numbers."""
    world = World([ListIssues(milestone="current")])
    await world.linked()
    await _issues(world)
    world.codehost.milestones[SANDBOX.key] = list(milestones)
    for number, milestone in (in_milestone or {}).items():
        key = (SANDBOX.key, number)
        world.codehost.issues[key] = world.codehost.issues[key].model_copy(
            update={"milestone": milestone}
        )
    return world


def listed(text: str) -> list[int]:
    return [int(line.split("#")[1].split()[0]) for line in text.splitlines()[1:]]


async def test_this_sprint_is_the_open_milestone_due_soonest() -> None:
    world = await team(SPRINT_12, V2, in_milestone={1: 2, 2: 3})
    text = await world.say("what's open this sprint?")
    assert text.splitlines()[0] == messages.ISSUES_TITLE.format(
        state="Open", repo=SANDBOX.full_name, filters=" in Sprint 12 (due 20 Nov)"
    )
    assert listed(text) == [1]


async def test_an_overdue_sprint_that_is_still_open_is_still_the_current_one() -> None:
    world = await team(SPRINT_12, SPRINT_11, in_milestone={1: 2, 3: 1})
    text = await world.say("what's open this sprint?")
    assert "Sprint 11 (due 1 Nov)" in text.splitlines()[0]
    assert listed(text) == [3]


async def test_a_single_open_milestone_without_a_due_date_is_the_current_one() -> None:
    world = await team(V2, in_milestone={2: 3})
    text = await world.say("what's open this sprint?")
    assert text.splitlines()[0].endswith(" in v2.0:")
    assert listed(text) == [2]


async def test_several_open_milestones_without_dates_bring_a_question() -> None:
    other = Milestone(number=4, title="Backlog")
    world = await team(V2, other)
    text = await world.say("what's open this sprint?")
    assert text == messages.WHICH_MILESTONE.format(names="v2.0, Backlog")


async def test_no_open_milestone_is_said_plainly() -> None:
    world = await team()
    text = await world.say("what's open this sprint?")
    assert text == messages.NO_MILESTONE.format(repo=SANDBOX.full_name)


async def test_a_named_milestone_is_found_whatever_its_case() -> None:
    world = await team(SPRINT_12, V2, in_milestone={1: 2, 2: 3})
    world.llm.answers = [[ListIssues(milestone="V2.0")]]
    text = await world.say("what's left in v2.0?")
    assert text.splitlines()[0].endswith(" in v2.0:")
    assert listed(text) == [2]


async def test_an_unknown_milestone_brings_a_question_with_the_open_ones() -> None:
    world = await team(SPRINT_12, V2)
    world.llm.answers = [[ListIssues(milestone="v3")]]
    text = await world.say("what's left in v3?")
    assert text == messages.MILESTONE_UNKNOWN.format(
        name="v3", repo=SANDBOX.full_name, names="Sprint 12, v2.0"
    )


async def test_a_list_assigned_to_me_this_sprint_is_mine_and_in_the_sprint() -> None:
    world = await team(SPRINT_12, in_milestone={1: 2, 2: 2, 3: 2})
    world.llm.answers = [[ListIssues(assignee="me", milestone="current")]]
    text = await world.say("what's on my plate this sprint?")
    assert listed(text) == [1, 3]  # #2 is Annie's
    assert "assigned to tekeufranck681" in text.splitlines()[0]


@pytest.mark.parametrize("words", ["this sprint", "Current Sprint", "current milestone", "sprint"])
async def test_this_sprint_phrasings_all_mean_the_current_one(words: str) -> None:
    world = await team(SPRINT_12, in_milestone={1: 2})
    world.llm.answers = [[ListIssues(milestone=words)]]
    assert listed(await world.say("what's open?")) == [1]


async def test_a_milestone_title_is_shown_cleaned() -> None:
    sneaky = Milestone(number=5, title="Sprint‮ 13​", due_on=date(2026, 12, 4))
    world = await team(sneaky, in_milestone={1: 5})
    heading = (await world.say("what's open this sprint?")).splitlines()[0]
    assert "‮" not in heading and "​" not in heading
    assert "Sprint 13 (due 4 Dec)" in heading


async def test_my_issues_can_be_limited_to_the_sprint_too() -> None:
    world = await team(SPRINT_12, in_milestone={1: 2, 2: 2})  # #3 is Franck's but not in it
    world.llm.answers = [[MyIssues(milestone="current")]]
    text = await world.say("what's on my plate this sprint?")
    assert listed(text) == [1]
    assert "in Sprint 12 (due 20 Nov)" in text.splitlines()[0]


async def test_my_issues_in_a_named_milestone_without_a_due_date() -> None:
    world = await team(SPRINT_12, V2, in_milestone={1: 3, 3: 2})
    world.llm.answers = [[MyIssues(milestone="v2.0")]]
    text = await world.say("what's mine in v2.0?")
    assert text.splitlines()[0].endswith(" in v2.0:")
    assert listed(text) == [1]


async def test_a_milestone_actually_named_sprint_is_found_by_its_name() -> None:
    named = Milestone(number=6, title="Sprint")
    world = await team(SPRINT_12, named, in_milestone={1: 2, 2: 6})
    world.llm.answers = [[ListIssues(milestone="sprint")]]
    assert listed(await world.say("what's in the Sprint milestone?")) == [2]


async def test_two_open_milestones_with_the_same_title_bring_a_question() -> None:
    twin = Milestone(number=7, title="v2.0 ")
    world = await team(V2, twin)
    world.llm.answers = [[ListIssues(milestone="v2.0")]]
    text = await world.say("what's left in v2.0?")
    assert text == messages.MILESTONE_DUPLICATE.format(count=2, name="v2.0", repo=SANDBOX.full_name)


async def test_github_trouble_while_listing_milestones_is_said_plainly() -> None:
    world = await team(SPRINT_12)

    async def down(*_: object) -> list[Milestone]:
        raise CodeHostUnavailableError("down")

    world.codehost.list_milestones = down  # type: ignore[method-assign]
    with pytest.raises(CodeHostUnavailableError):
        await world.say("what's open this sprint?")
    assert world.messaging.sent[-1][1] == messages.GITHUB_TROUBLE


async def test_the_milestone_questions_stay_short() -> None:
    long_ones = [Milestone(number=n, title="M" * 300 + str(n)) for n in range(10, 20)]
    world = await team(*long_ones)
    world.llm.answers = [[ListIssues(milestone="nope")]]
    assert len(await world.say("what's left in nope?")) <= 500
