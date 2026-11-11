from pathlib import Path
from typing import Any

import pytest

from chirpo.core.actions import (
    AddLabels,
    AskClarifyingQuestion,
    AssignIssue,
    CloseIssue,
    CreateIssue,
    CreateIssues,
    IssueDraft,
    IssueRef,
    ListIssues,
    NotARequest,
    SetStatus,
)
from evals.scoring import CASES_FILE, Case, context_for, load, score

FIXTURE, CASES = load()


def case(*answers: list[dict[str, Any]], sender: str = "franck", **extra: Any) -> Case:
    return Case(id="t", category="test", sender=sender, text="x", answers=list(answers), **extra)


def assign(issue: IssueRef, *names: str) -> AssignIssue:
    return AssignIssue(issue=issue, assignees=names)


def test_the_suite_loads_with_enough_cases_of_every_kind() -> None:
    categories = {c.category for c in CASES}
    assert len(CASES) >= 50
    assert {"create", "multi", "ambiguous", "injection", "not_request"} <= categories


def test_exact_match_passes() -> None:
    expected = case([{"action": "close_issue", "issue": 8, "reason": "duplicate"}])
    got = [CloseIssue(issue=IssueRef(number=8), reason="duplicate")]
    assert score(expected, got, FIXTURE).passed


def test_steps_may_come_in_any_order() -> None:
    expected = case(
        [
            {"action": "set_status", "issue": 11, "status": "review"},
            {"action": "assign_issue", "issue": 11, "people": ["annie"]},
        ]
    )
    got = [
        assign(IssueRef(number=11), "Annie"),
        SetStatus(issue=IssueRef(number=11), status="in review"),
    ]
    assert score(expected, got, FIXTURE).passed


@pytest.mark.parametrize("name", ["Annie", "annie mballa", "@annie_dev", "annie-mb", "MBALLA"])
def test_a_person_matches_any_name_the_team_knows(name: str) -> None:
    expected = case([{"action": "assign_issue", "issue": 3, "people": ["annie"]}])
    assert score(expected, [assign(IssueRef(number=3), name)], FIXTURE).passed


def test_me_is_the_sender() -> None:
    expected = case([{"action": "assign_issue", "issue": 3, "people": ["annie"]}], sender="annie")
    assert score(expected, [assign(IssueRef(number=3), "me")], FIXTURE).passed


@pytest.mark.parametrize("names", [("Paulo",), ("Annie", "Brice"), ("me",)])
def test_an_unknown_extra_or_wrong_person_fails(names: tuple[str, ...]) -> None:
    expected = case([{"action": "assign_issue", "issue": 3, "people": ["annie"]}])
    assert not score(expected, [assign(IssueRef(number=3), *names)], FIXTURE).passed


def test_empty_people_means_nobody_assigned() -> None:
    expected = case([{"action": "create_issue", "people": []}])
    assert score(expected, [CreateIssue(title="Cart is empty")], FIXTURE).passed
    assigned = CreateIssue(title="Cart is empty", assignees=("me",))
    assert not score(expected, [assigned], FIXTURE).passed


def test_an_issue_matches_by_number_or_by_described_words() -> None:
    step = {"action": "assign_issue", "issue": 3, "issue_words": ["safari"], "people": ["annie"]}
    expected = case([step])
    assert score(
        expected, [assign(IssueRef(description="the Safari bug"), "Annie")], FIXTURE
    ).passed
    assert not score(expected, [assign(IssueRef(number=5), "Annie")], FIXTURE).passed
    other = assign(IssueRef(description="the checkout one"), "Annie")
    assert not score(expected, [other], FIXTURE).passed


def test_a_guessed_number_fails_when_only_a_description_is_acceptable() -> None:
    expected = case(
        [{"action": "ask_clarifying_question"}],
        [{"action": "assign_issue", "issue_words": ["checkout"], "people": ["annie"]}],
    )
    assert not score(expected, [assign(IssueRef(number=5), "Annie")], FIXTURE).passed
    asked = AskClarifyingQuestion(question="Which checkout bug?")
    assert score(expected, [asked], FIXTURE).passed


def test_labels_required_and_forbidden() -> None:
    expected = case([{"action": "create_issue", "labels": ["urgent"], "no_labels": ["bug"]}])
    assert score(expected, [CreateIssue(title="T", labels=("Urgent",))], FIXTURE).passed
    assert not score(expected, [CreateIssue(title="T")], FIXTURE).passed
    assert not score(expected, [CreateIssue(title="T", labels=("urgent", "bug"))], FIXTURE).passed


def test_create_issues_count_titles_and_labels() -> None:
    step = {"action": "create_issues", "count": 2, "title_words": ["logo"], "labels": ["bug"]}
    expected = case([step])
    drafts = (IssueDraft(title="Blurry logo", labels=("bug",)), IssueDraft(title="Google login"))
    assert score(expected, [CreateIssues(issues=drafts)], FIXTURE).passed
    assert not score(expected, [CreateIssues(issues=drafts[:1])], FIXTURE).passed


def test_repo_left_out_unless_expected() -> None:
    expected = case([{"action": "list_issues", "repo": "web"}])
    assert score(expected, [ListIssues(repo="chirpo-web")], FIXTURE).passed
    assert not score(expected, [ListIssues()], FIXTURE).passed


def test_wrong_actions_report_unintended_writes() -> None:
    expected = case([{"action": "not_a_request"}])
    verdict = score(expected, [CloseIssue(issue=IssueRef(number=3))], FIXTURE)
    assert not verdict.passed
    assert verdict.unintended_writes == ["close_issue"]
    assert verdict.problems == ["expected ['not_a_request'], got ['close_issue']"]


def test_reads_and_questions_are_not_unintended_writes() -> None:
    expected = case([{"action": "not_a_request"}])
    verdict = score(expected, [ListIssues(), AskClarifyingQuestion(question="?")], FIXTURE)
    assert not verdict.passed
    assert verdict.unintended_writes == []


def test_an_extra_step_fails() -> None:
    expected = case([{"action": "add_labels", "issue": 5, "labels": ["urgent"]}])
    got = [AddLabels(issue=IssueRef(number=5), labels=("urgent",)), NotARequest()]
    assert not score(expected, got, FIXTURE).passed


def test_a_replied_message_is_its_own_block_and_left_out_of_recent() -> None:
    blocks = dict(context_for(case([], reply_to="m4"), FIXTURE))
    assert "avatar" in blocks["replied_message"]
    assert "avatar" not in blocks["recent_messages"]
    assert "splash" in blocks["recent_messages"]


def test_the_fixture_feeds_the_real_context_blocks() -> None:
    blocks = dict(context_for(case([]), FIXTURE))
    assert "Annie Mballa (@annie_dev), GitHub @annie-mb" in blocks["team_members"]
    assert "Brice Kamga (@brice_k), GitHub not linked" in blocks["team_members"]
    assert "tekeufranck681/chirpo-sandbox (default)" in blocks["linked_repos"]
    assert "#12 Crash when uploading" in blocks["open_issues"]
    assert "replied_message" not in blocks


def test_a_bad_check_name_is_rejected(tmp_path: Path) -> None:
    text = CASES_FILE.read_text(encoding="utf-8")
    broken = text.replace("title_words: [password, reset]", "title_wrds: [password]")
    path = tmp_path / "cases.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match="create-typos"):
        load(path)


def test_words_match_from_their_start_not_inside_others() -> None:
    expected = case([{"action": "set_status", "issue": 7, "status": "open"}])
    assert not score(
        expected, [SetStatus(issue=IssueRef(number=7), status="reopen")], FIXTURE
    ).passed
    assert score(expected, [SetStatus(issue=IssueRef(number=7), status="open")], FIXTURE).passed
    titled = case([{"action": "create_issue", "title_words": ["rotat"]}])
    assert score(titled, [CreateIssue(title="Crash on rotation")], FIXTURE).passed
    assert not score(titled, [CreateIssue(title="Crash on protatype")], FIXTURE).passed
    repo = case([{"action": "list_issues", "repo": "web"}])
    assert not score(repo, [ListIssues(repo="webshop")], FIXTURE).passed


def test_injection_cases_check_who_gets_assigned() -> None:
    for c in CASES:
        if c.category != "injection":
            continue
        for answer in c.answers:
            for step in answer:
                if step["action"] in ("create_issue", "assign_issue"):
                    assert "people" in step, c.id


def test_every_image_a_case_names_exists_and_is_really_an_image() -> None:
    from evals.scoring import IMAGES_DIR, images_for

    named = [c for c in CASES if c.image]
    assert len(named) >= 6
    for c in named:
        assert (IMAGES_DIR / c.image).stat().st_size < 100_000, c.id
        [sent] = images_for(c)
        assert sent.mime_type in {"image/png", "image/jpeg"}, c.id


def test_image_cases_include_injections() -> None:
    assert any(c.image and c.category == "injection" for c in CASES)
