"""How a model answer is compared with what a case expects (M4.9).

Only the model is scored, not resolution: people stay as the message names them, so an expected
person ("annie") matches any name the team fixture knows for her (display name or one of its
words, @username, GitHub login), and "me" matches the sender.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from chirpo.core.actions import ACTIONS, IssueRef, NotARequest, Proposal, SmallTalk, spec_of
from chirpo.core.context import Block, Team, context_blocks
from chirpo.core.domain import (
    Attachment,
    AttachmentKind,
    ChatUser,
    Issue,
    IssueState,
    RecentMessage,
    RepoRef,
)
from chirpo.core.images import sniff
from chirpo.core.llm import IMAGE_OWN, IMAGE_RECENT, IMAGE_REPLIED, RequestImage
from chirpo.core.pending import Step as PendingStep
from chirpo.core.permissions import Action, Role
from chirpo.core.workspaces import CodeHostAccount, LinkedRepo, Member

CASES_FILE = Path(__file__).with_name("cases.yaml")
IMAGES_DIR = Path(__file__).with_name("images")
_SELF = frozenset({"me", "myself", "i"})
_CHECKS = frozenset(
    {
        "action",
        "people",
        "issue",
        "issue_words",
        "labels",
        "no_labels",
        "status",
        "reason",
        "repo",
        "state",
        "title_words",
        "count",
    }
)

Step = Mapping[str, Any]


@dataclass(frozen=True)
class Fixture:
    team: Team
    aliases: Mapping[str, frozenset[str]]  # member key -> names that mean them
    names: Mapping[str, str]  # member key -> display name
    recent: Sequence[RecentMessage]


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    sender: str  # member key
    text: str
    answers: Sequence[Sequence[Step]]  # any one of these lists of steps passes
    reply_to: str | None = None
    image: str | None = None  # a file in evals/images, sent with the message
    image_from: str = "own"  # "own": attached to the message; "reply": to the replied-to one
    # A screenshot Annie sent two minutes ago, which the model may ask to see (D52); `look` says
    # whether it must (true) or must not (false) ask.
    recent_image: str | None = None
    recent_image_text: str = ""
    look: bool | None = None
    # The message replies to a card (M5.3) that proposes these steps (stored as they would be).
    pending: tuple[PendingStep, ...] = ()


@dataclass
class Verdict:
    passed: bool
    problems: list[str] = field(default_factory=list)
    unintended_writes: list[str] = field(default_factory=list)


def load(path: Path = CASES_FILE) -> tuple[Fixture, list[Case]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fixture = _fixture(data["team"])
    cases = [_case(raw, fixture) for raw in data["cases"]]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique")
    return fixture, cases


RECENT_IMAGE_ID = "eval-recent-image"


def look_candidate(case: Case, fixture: Fixture) -> Attachment | None:
    """The image of the case's recent screenshot, which the model may ask to see."""
    if case.recent_image is None:
        return None
    data = (IMAGES_DIR / case.recent_image).read_bytes()
    return Attachment(
        kind=AttachmentKind.IMAGE,
        file_id=f"eval:{case.recent_image}",
        mime_type=sniff(data),
        size=len(data),
    )


def fetch_eval_image(attachment: Attachment) -> RequestImage:
    data = (IMAGES_DIR / attachment.file_id.removeprefix("eval:")).read_bytes()
    mime_type = sniff(data)
    if mime_type is None:
        raise ValueError(f"{attachment.file_id} isn't a JPEG, PNG or WebP")
    return RequestImage(label=IMAGE_RECENT, mime_type=mime_type, data=data)


def context_for(case: Case, fixture: Fixture) -> list[Block]:
    recent, replied = list(fixture.recent), None
    if case.reply_to is not None:
        replied = next(m for m in recent if m.message_id == case.reply_to)
        recent = [m for m in recent if m.message_id != case.reply_to]
    candidate = look_candidate(case, fixture)
    if candidate is not None:
        recent.append(
            RecentMessage(
                message_id=RECENT_IMAGE_ID,
                sender=ChatUser(
                    platform="telegram", user_id="annie", display_name=fixture.names["annie"]
                ),
                text=case.recent_image_text,
                sent_at=datetime.now(UTC) - timedelta(minutes=2),
                attachments=(candidate,),
            )
        )
    return context_blocks(
        fixture.team,
        recent=recent,
        replied=replied,
        media=candidate is not None,
        look_at=RECENT_IMAGE_ID if candidate else None,
        proposal=case.pending or None,
    )


def images_for(case: Case) -> list[RequestImage]:
    if case.image is None:
        return []
    data = (IMAGES_DIR / case.image).read_bytes()
    mime_type = sniff(data)
    if mime_type is None:
        raise ValueError(f"case {case.id}: {case.image} isn't a JPEG, PNG or WebP")
    label = IMAGE_REPLIED if case.image_from == "reply" else IMAGE_OWN
    return [RequestImage(label=label, mime_type=mime_type, data=data)]


def sender_name(case: Case, fixture: Fixture) -> str:
    return fixture.names[case.sender]


def score(case: Case, proposals: Sequence[Proposal], fixture: Fixture) -> Verdict:
    closest: list[str] | None = None
    for answer in case.answers:
        problems = _match_answer(answer, proposals, case, fixture)
        if not problems:
            return Verdict(passed=True)
        if closest is None or len(problems) < len(closest):
            closest = problems
    # A pass already means exactly the expected actions, so only failures can hold extra writes.
    expected = {step["action"] for answer in case.answers for step in answer}
    unintended = [
        spec_of(p).name for p in proposals if _writes(p) and spec_of(p).name not in expected
    ]
    return Verdict(passed=False, problems=closest or [], unintended_writes=unintended)


def describe(proposal: Proposal) -> str:
    fields = proposal.model_dump(exclude_defaults=True, mode="json")
    return f"{spec_of(proposal).name} {fields}" if fields else spec_of(proposal).name


def _writes(proposal: Proposal) -> bool:
    return spec_of(proposal).permission not in (None, Action.VIEW)


def _match_answer(
    answer: Sequence[Step], proposals: Sequence[Proposal], case: Case, fx: Fixture
) -> list[str]:
    want = [step["action"] for step in answer]
    if "small_talk" not in want:
        proposals = _without_small_talk(proposals)
    got = [spec_of(p).name for p in proposals]
    if sorted(got) != sorted(want):
        return [f"expected {want}, got {got}"]
    fewest: list[str] | None = None
    for order in _orders(len(proposals)):
        problems = [
            problem
            for step, index in zip(answer, order, strict=True)
            for problem in _check(step, proposals[index], case, fx)
        ]
        if not problems:
            return []
        if fewest is None or len(problems) < len(fewest):
            fewest = problems
    return fewest or []


def _orders(size: int) -> Iterator[tuple[int, ...]]:
    """Steps may come in any order; past six, only the asked order is tried."""
    yield tuple(range(size))
    if size <= 6:
        yield from permutations(range(size))


def _without_small_talk(proposals: Sequence[Proposal]) -> list[Proposal]:
    """Small talk is only text. Next to a request it is dropped (core/interpreter.py); alone it is
    the older "nothing to do" answer, which cases written before it was added still expect."""
    rest = [p for p in proposals if not isinstance(p, SmallTalk)]
    if len(rest) == len(proposals):
        return rest
    if all(isinstance(p, NotARequest) for p in rest):
        return [NotARequest()]
    return rest


_LINK = re.compile(r"https?://|www\.|\bt\.me/", re.IGNORECASE)
_CLAIM = re.compile(
    r"\b(clos(?:ed|ing)|assign(?:ed|ing)|creat(?:ed|ing)|delet(?:ed|ing)|merg(?:ed|ing))\b", re.I
)


def _small_talk_problems(reply: str) -> list[str]:
    """Small talk can't act, so it mustn't mention anyone, link anywhere or claim an action."""
    problems = []
    if "@" in reply:
        problems.append(f"small_talk: mentions someone: {reply!r}")
    if _LINK.search(reply):
        problems.append(f"small_talk: has a link: {reply!r}")
    if claim := _CLAIM.search(reply):
        problems.append(f"small_talk: claims an action ({claim.group(0)}): {reply!r}")
    return problems


def _check(step: Step, proposal: Proposal, case: Case, fx: Fixture) -> list[str]:
    name, problems = step["action"], []
    if spec_of(proposal).name != name:
        return [f"expected {name}, got {spec_of(proposal).name}"]
    values = dict(proposal)
    if name == "small_talk":
        return _small_talk_problems(str(values["reply"]))
    if "people" in step:
        written = _people(values)
        if not _same_people(step["people"], written, case.sender, fx):
            problems.append(f"{name}: people {list(written)}, expected {step['people']}")
    if "issue" in step or "issue_words" in step:
        issue = values["issue"]
        if not _same_issue(step.get("issue"), step.get("issue_words", ()), issue):
            problems.append(f"{name}: issue {issue.model_dump(exclude_none=True)}")
    labels = {label.casefold() for label in _labels(values)}
    missing = {label.casefold() for label in step.get("labels", ())} - labels
    if missing:
        problems.append(f"{name}: labels {sorted(labels)}, missing {sorted(missing)}")
    unwanted = labels & {label.casefold() for label in step.get("no_labels", ())}
    if unwanted:
        problems.append(f"{name}: unwanted labels {sorted(unwanted)}")
    if "status" in step and not _has_word([values["status"]], [step["status"]]):
        problems.append(f"{name}: status {values['status']!r}, expected {step['status']!r}")
    if "reason" in step and str(values["reason"]) != step["reason"]:
        problems.append(f"{name}: reason {values['reason']}, expected {step['reason']}")
    if "repo" in step and not _same_repo(step["repo"], values.get("repo")):
        problems.append(f"{name}: repo {values.get('repo')!r}, expected {step['repo']!r}")
    if "state" in step and values["state"] != step["state"]:
        problems.append(f"{name}: state {values['state']}, expected {step['state']}")
    if "title_words" in step and not _has_word(_titles(values), step["title_words"]):
        problems.append(f"{name}: titles {_titles(values)} lack any of {step['title_words']}")
    if "count" in step and len(values["issues"]) != step["count"]:
        problems.append(f"{name}: {len(values['issues'])} issues, expected {step['count']}")
    return problems


def _people(values: Mapping[str, Any]) -> tuple[str, ...]:
    if "assignees" in values:
        return tuple(values["assignees"])
    return (values["assignee"],) if values.get("assignee") else ()


def _same_people(expected: Sequence[str], written: Sequence[str], sender: str, fx: Fixture) -> bool:
    remaining = list(expected)
    for name in written:
        key = _person(name, sender, fx)
        if key not in remaining:
            return False
        remaining.remove(key)
    return not remaining


def _person(name: str, sender: str, fx: Fixture) -> str | None:
    word = name.strip().removeprefix("@").casefold()
    if word in _SELF:
        return sender
    return next((key for key, names in fx.aliases.items() if word in names), None)


def _same_issue(number: int | None, words: Sequence[str], issue: IssueRef) -> bool:
    if issue.number is not None:
        return issue.number == number
    return issue.description is not None and _has_word([issue.description], words)


def _same_repo(expected: str | None, written: str | None) -> bool:
    if expected is None:
        return written is None
    return (
        written is not None
        and re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", written, re.IGNORECASE) is not None
    )


def _labels(values: Mapping[str, Any]) -> list[str]:
    labels = list(values.get("labels", ()))
    for draft in values.get("issues", ()):
        labels += draft.labels
    return labels


def _titles(values: Mapping[str, Any]) -> list[str]:
    if "issues" in values:
        return [draft.title for draft in values["issues"]]
    return [values["title"]] if "title" in values else []


def _has_word(texts: Sequence[str], words: Sequence[str]) -> bool:
    """Any of `words` starting a word: "rotat" finds "rotation"; "open" doesn't find "reopen"."""
    text = " ".join(texts)
    return any(re.search(rf"(?<!\w){re.escape(word)}", text, re.IGNORECASE) for word in words)


def _fixture(raw: Mapping[str, Any]) -> Fixture:
    workspace = uuid4()
    members, aliases, names = [], {}, {}
    for key, person in raw["members"].items():
        login = person.get("login")
        account = (
            CodeHostAccount(provider="github", user_id=f"id-{key}", login=login) if login else None
        )
        members.append(
            Member(
                id=uuid4(),
                workspace_id=workspace,
                display_name=person["name"],
                role=Role(person.get("role", "member")),
                chat_username=person.get("username"),
                codehost=account,
            )
        )
        known = {person["name"], *person["name"].split(), person.get("username"), login}
        aliases[key] = frozenset(name.casefold() for name in known if name)
        names[key] = person["name"]
    repos = [
        LinkedRepo(repo=_repo(full_name), is_default=index == 0)
        for index, full_name in enumerate(raw["repos"])
    ]
    default, now = repos[0].repo, datetime.now(UTC)
    issues = [
        Issue(
            repo=default,
            number=issue["number"],
            title=issue["title"],
            state=IssueState.OPEN,
            assignees=tuple(issue.get("assignees", ())),
            labels=tuple(issue.get("labels", ())),
            url=f"https://github.com/{default.full_name}/issues/{issue['number']}",
            created_at=now,
            updated_at=now,
        )
        for issue in raw["issues"]
    ]
    recent = [
        RecentMessage(
            message_id=message["id"],
            sender=ChatUser(
                platform="telegram", user_id=message["from"], display_name=names[message["from"]]
            ),
            text=message["text"],
            sent_at=now - timedelta(minutes=len(raw["recent"]) - index),
        )
        for index, message in enumerate(raw["recent"])
    ]
    return Fixture(Team(members, repos, issues), aliases, names, recent)


def _case(raw: Mapping[str, Any], fx: Fixture) -> Case:
    """`expect`: the one right list of steps; `any_of`: several acceptable lists."""
    answers = raw["any_of"] if "any_of" in raw else [raw["expect"]]
    for answer in answers:
        for step in answer:
            if set(step) - _CHECKS or step.get("action") not in ACTIONS:
                raise ValueError(f"case {raw['id']}: bad step {dict(step)}")
    if raw["from"] not in fx.names:
        raise ValueError(f"case {raw['id']}: unknown sender {raw['from']}")
    reply_to = raw.get("reply_to")
    if reply_to is not None and reply_to not in {m.message_id for m in fx.recent}:
        raise ValueError(f"case {raw['id']}: no recent message {reply_to}")
    image, image_from = raw.get("image"), raw.get("image_from", "own")
    if image is not None and not (IMAGES_DIR / image).is_file():
        raise ValueError(f"case {raw['id']}: no image {image}")
    if image_from not in ("own", "reply"):
        raise ValueError(f"case {raw['id']}: image_from is own or reply")
    recent_image = raw.get("recent_image")
    if recent_image is not None and not (IMAGES_DIR / recent_image).is_file():
        raise ValueError(f"case {raw['id']}: no image {recent_image}")
    return Case(
        id=raw["id"],
        category=raw["category"],
        sender=raw["from"],
        text=raw["text"],
        answers=answers,
        reply_to=reply_to,
        image=image,
        image_from=image_from,
        recent_image=recent_image,
        recent_image_text=raw.get("recent_image_text", ""),
        look=raw.get("look"),
        pending=_pending_steps(raw.get("pending", ()), fx, raw["id"]),
    )


def _pending_steps(
    raw: Sequence[Mapping[str, Any]], fx: Fixture, case_id: str
) -> tuple[PendingStep, ...]:
    """The card's steps as the interpreter stores them: validated, in the default repo."""
    repo = fx.team.default_repo
    steps = []
    for entry in raw:
        fields = {key: value for key, value in entry.items() if key != "action"}
        try:
            proposal = ACTIONS[entry["action"]].model.model_validate(
                {"repo": repo.full_name, **fields}
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"case {case_id}: bad card step {dict(entry)}") from error
        steps.append(PendingStep.of(proposal, repo))
    return tuple(steps)


def _repo(full_name: str) -> RepoRef:
    owner, name = full_name.split("/")
    return RepoRef(provider="github", owner=owner, name=name)
