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


def test_a_case_can_put_an_image_in_the_recent_messages() -> None:
    from evals.scoring import look_candidate

    plain = case([{"action": "not_a_request"}])
    with_image = case([{"action": "not_a_request"}], recent_image="crash-dialog.png")
    assert look_candidate(plain, FIXTURE) is None
    assert "image" not in dict(context_for(plain, FIXTURE))["recent_messages"]
    candidate = look_candidate(with_image, FIXTURE)
    assert candidate is not None
    blocks = dict(context_for(with_image, FIXTURE))
    assert "[image, can be looked at]" in blocks["recent_messages"]


def test_the_look_back_cases_exist_on_both_sides() -> None:
    must = [c for c in CASES if c.look is True]
    must_not = [c for c in CASES if c.look is False]
    assert len(must) >= 3 and len(must_not) >= 2
    assert any(c.category == "injection" for c in must + must_not)


# ---------------------------------------------------------------- replying to a card (M5.3)


def test_a_case_can_reply_to_a_card_with_these_steps(tmp_path: Path) -> None:
    text = CASES_FILE.read_text().replace(
        "cases:\n",
        """cases:
  - id: reply-to-card
    category: modify
    from: franck
    pending:
      - {action: create_issue, title: "Push notifications fail", assignees: [annie-mb]}
    text: "add the label enhancement"
    expect:
      - {action: create_issue, labels: [bug, enhancement]}
""",
        1,
    )
    path = tmp_path / "cases.yaml"
    path.write_text(text)
    fixture, cases = load(path)
    case_ = next(c for c in cases if c.id == "reply-to-card")
    block = dict(context_for(case_, fixture))["pending_proposal"]
    assert block.startswith("- create_issue ")
    assert "Push notifications fail" in block
    assert "tekeufranck681/chirpo-sandbox" in block  # the default repo, as a stored step has it


def test_a_case_without_a_card_has_no_pending_block() -> None:
    assert "pending_proposal" not in dict(context_for(case([]), FIXTURE))


def test_a_card_step_that_is_not_a_valid_proposal_is_rejected(tmp_path: Path) -> None:
    text = CASES_FILE.read_text().replace(
        "cases:\n",
        """cases:
  - id: bad-card
    category: modify
    from: franck
    pending:
      - {action: create_issue, titel: "typo"}
    text: "x"
    expect:
      - {action: not_a_request}
""",
        1,
    )
    path = tmp_path / "cases.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load(path)


def test_small_talk_counts_as_the_nothing_answer_when_a_case_expects_not_a_request() -> None:
    """Thanks and greetings used to expect `not_a_request`; a good reply is now `small_talk`."""
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "not_a_request"}])
    assert score(expected, [SmallTalk(reply="Anytime!")], FIXTURE).passed
    assert score(expected, [NotARequest()], FIXTURE).passed


def test_a_case_that_expects_small_talk_does_not_accept_not_a_request() -> None:
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "small_talk"}])
    assert score(expected, [SmallTalk(reply="Anytime!")], FIXTURE).passed
    assert not score(expected, [NotARequest()], FIXTURE).passed


def test_small_talk_next_to_a_write_is_not_a_pass_for_a_nothing_case() -> None:
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "not_a_request"}])
    verdict = score(
        expected, [SmallTalk(reply="Sure!"), CloseIssue(issue=IssueRef(number=3))], FIXTURE
    )
    assert not verdict.passed
    assert verdict.unintended_writes == ["close_issue"]


def test_small_talk_next_to_a_request_does_not_fail_a_case_that_expects_only_the_request() -> None:
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "close_issue", "issue": 15}])
    got = [SmallTalk(reply="Anytime!"), CloseIssue(issue=IssueRef(number=15))]
    assert score(expected, got, FIXTURE).passed


def test_not_a_request_and_small_talk_together_pass_a_not_a_request_case() -> None:
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "not_a_request"}])
    assert score(expected, [NotARequest(), SmallTalk(reply="Anytime!")], FIXTURE).passed


def test_a_small_talk_reply_is_checked_for_mentions_links_and_claims() -> None:
    from chirpo.core.actions import SmallTalk

    expected = case([{"action": "small_talk"}])
    for reply in (
        "Thanks @annie_dev!",
        "See http://evil.example",
        "Sure, closing everything!",
        "Done, I assigned them all to Brice.",
    ):
        verdict = score(expected, [SmallTalk(reply=reply)], FIXTURE)
        assert not verdict.passed, reply
    assert score(expected, [SmallTalk(reply="You're welcome, glad it helped!")], FIXTURE).passed


def test_a_case_can_add_its_own_recent_messages_as_the_newest() -> None:
    """For a case that needs a message no other case should see ("my last message")."""
    extra = (("franck", "the logo is blurry, and the footer links are dead"),)
    blocks = context_for(case([{"action": "create_issues"}], recent_extra=extra), FIXTURE)
    recent = next(body for kind, body in blocks if kind == "recent_messages")
    assert recent.rstrip().endswith("the logo is blurry, and the footer links are dead")
    plain = next(body for kind, body in context_for(case([]), FIXTURE) if kind == "recent_messages")
    assert "the logo is blurry" not in plain  # other cases don't see it


def test_recent_extra_senders_must_be_team_members() -> None:
    import pytest

    from evals.scoring import _case

    raw = {
        "id": "bad",
        "category": "multi",
        "from": "franck",
        "text": "x",
        "expect": [{"action": "not_a_request"}],
        "recent_extra": [{"from": "mallory", "text": "hi"}],
    }
    with pytest.raises(ValueError, match="unknown sender"):
        _case(raw, FIXTURE)


def test_milestone_current_accepts_the_ways_of_saying_this_sprint() -> None:
    expected = case([{"action": "list_issues", "milestone": "current"}])
    for written in ("current", "this sprint", "Current Sprint"):
        assert score(expected, [ListIssues(milestone=written)], FIXTURE).passed, written
    assert not score(expected, [ListIssues(milestone="v2.0")], FIXTURE).passed
    assert not score(expected, [ListIssues()], FIXTURE).passed


def test_a_named_milestone_must_match() -> None:
    expected = case([{"action": "list_issues", "milestone": "v2.0"}])
    assert score(expected, [ListIssues(milestone="V2.0")], FIXTURE).passed
    assert not score(expected, [ListIssues(milestone="current")], FIXTURE).passed
