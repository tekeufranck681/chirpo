"""Plain-language requests end to end against fakes: context, model, resolution, dry-run reply."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from chirpo.core import messages
from chirpo.core.actions import (
    AskClarifyingQuestion,
    AssignIssue,
    CreateIssue,
    IssueRef,
    ListIssues,
    MyIssues,
    NotARequest,
    Proposal,
    SetStatus,
)
from chirpo.core.commands import Services
from chirpo.core.domain import (
    Attachment,
    AttachmentKind,
    AuditResult,
    ChatUser,
    NewIssue,
    RepoPermission,
    RepoRef,
)
from chirpo.core.errors import (
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusedError,
    LLMUnavailableError,
)
from chirpo.core.permissions import PermissionChecker
from chirpo.core.pipeline import handle_inbound
from chirpo.core.ratelimit import Limits, RateLimiter
from chirpo.core.workspaces import CodeHostAccount
from tests.fakes import (
    CHAT,
    USER,
    FakeAuditLog,
    FakeCodeHost,
    FakeCounters,
    FakeLLM,
    FakeMembers,
    FakeMessageBuffer,
    FakeMessaging,
    FakeWorkspaces,
    make_message,
)

SANDBOX = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
FRANCK_GITHUB = CodeHostAccount(provider="github", user_id="186301755", login="tekeufranck681")
ANNIE = ChatUser(platform="fake", user_id="77", display_name="Annie Ngo", username="annie_n")
ANNIE_GITHUB = CodeHostAccount(provider="github", user_id="2", login="annie-gh")
# In this file the fake chat's USER is Franck.
FRANCK = USER.model_copy(update={"display_name": "Tekeu Franck", "username": "franck_tg"})


class World:
    def __init__(self, *answers: list[Proposal] | Exception) -> None:
        self.members = FakeMembers()
        self.workspaces = FakeWorkspaces(self.members)
        self.audit = FakeAuditLog()
        self.codehost = FakeCodeHost()
        self.codehost.provider = "github"
        self.codehost.logins = {"186301755": "tekeufranck681", "2": "annie-gh"}
        self.codehost.labels[SANDBOX.key] = {"bug", "enhancement"}
        self.buffer = FakeMessageBuffer(timedelta(hours=24))
        self.llm = FakeLLM(*answers)
        self.messaging = FakeMessaging()
        self.services = Services(
            workspaces=self.workspaces,
            members=self.members,
            audit=self.audit,
            codehost=self.codehost,
            permissions=PermissionChecker(self.codehost),
            buffer=self.buffer,
            context_hours=24,
            llm=self.llm,
        )

    async def linked(self, *, verified: bool = True) -> None:
        workspace = await self.workspaces.get_or_create_for_chat(CHAT, name="Shop")
        await self.workspaces.link_repo(workspace.id, SANDBOX)
        franck = await self.members.for_chat_user(workspace.id, FRANCK)
        if verified:
            await self.members.link_codehost(workspace.id, franck.id, FRANCK_GITHUB)
            self.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.WRITE
        annie = await self.members.for_chat_user(workspace.id, ANNIE)
        await self.members.link_codehost(workspace.id, annie.id, ANNIE_GITHUB)

    async def say(self, text: str, sender: ChatUser = FRANCK, **fields: object) -> str:
        self.messaging.sent.clear()
        message = make_message(text, sender=sender, mentions_bot=True, **fields)
        await handle_inbound(message, self.messaging, self.services)
        return self.messaging.sent[-1][1] if self.messaging.sent else ""


CHECKOUT = CreateIssue(
    title="Checkout crashes on Android 13",
    body="Paying by card crashes the app.",
    assignees=("Franck",),
    labels=("bug", "urgent"),
)


async def test_a_bug_report_becomes_a_dry_run() -> None:
    world = World([CHECKOUT])
    await world.linked()
    reply = await world.say("checkout crashes on Android 13, give it to Franck, it's urgent")
    assert reply == "\n".join(
        [
            messages.DRY_RUN_HEADER,
            '• Create an issue in tekeufranck681/chirpo-sandbox: "Checkout crashes on Android 13"',
            "  assigned to tekeufranck681",
            "  labels: bug",
            '  details: "Paying by card crashes the app."',
            "  ("
            + messages.LABELS_LEFT_OUT.format(labels='"urgent"', repo=SANDBOX.full_name)
            + ")",
        ]
    )
    assert world.messaging.typing == [CHAT]
    assert world.codehost.issues == {}  # a dry run: nothing created


async def test_actions_on_an_issue_show_its_title() -> None:
    world = World(
        [
            SetStatus(issue=IssueRef(number=1), status="in progress"),
            AssignIssue(issue=IssueRef(description="the checkout bug"), assignees=("Annie",)),
        ]
    )
    await world.linked()
    await world.codehost.create_issue(SANDBOX, NewIssue(title="Checkout crashes on Android 13"))
    reply = await world.say("move #1 to in progress and give the checkout bug to Annie")
    assert reply.splitlines()[1:] == [
        '• Move #1 "Checkout crashes on Android 13" to "in progress"',
        '• Assign annie-gh to #1 "Checkout crashes on Android 13"',
    ]


async def test_the_models_question_is_asked() -> None:
    world = World([AskClarifyingQuestion(question="Which issue should I assign to Franck?")])
    await world.linked()
    assert await world.say("assign it to Franck") == "Which issue should I assign to Franck?"


async def test_an_unclear_name_becomes_a_question() -> None:
    world = World([CHECKOUT.model_copy(update={"assignees": ("Kofi",)})])
    await world.linked()
    assert await world.say("give it to Kofi") == messages.PERSON_UNKNOWN.format(name="Kofi")


async def test_nothing_to_do() -> None:
    world = World([NotARequest()])
    await world.linked()
    assert await world.say("thanks chirpo!") == messages.NOT_A_REQUEST.format(bot="@chirpo_bot")
    assert "@chirpo_bot checkout crashes" in messages.NOT_A_REQUEST.format(bot="@chirpo_bot")


@pytest.mark.parametrize(
    ("error", "reply"),
    [
        (LLMRateLimitedError(17), messages.LLM_BUSY),
        (LLMRefusedError("SAFETY"), messages.LLM_REFUSED),
        (LLMInvalidOutputError(("no action proposed",)), messages.LLM_UNCLEAR),
    ],
)
async def test_model_failures_get_friendly_replies(error: Exception, reply: str) -> None:
    world = World(error)
    await world.linked()
    assert await world.say("checkout crashes") == reply


async def test_an_unreachable_model_is_reported_and_raised_for_the_logs() -> None:
    world = World(LLMUnavailableError("Gemini answered 503"))
    await world.linked()
    with pytest.raises(LLMUnavailableError):
        await world.say("checkout crashes")
    assert world.messaging.sent[-1][1] == messages.LLM_UNAVAILABLE


async def test_the_sender_must_be_allowed() -> None:
    world = World([CHECKOUT])
    await world.linked()
    world.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.READ
    reply = await world.say("checkout crashes, give it to Franck")
    assert "triage" in reply
    assert [(e.action, e.error) for e in world.audit.entries] == [
        ("triage_issue", "no_repo_access")
    ]


async def test_an_unlinked_sender_is_sent_to_iam_without_calling_the_model() -> None:
    world = World([CHECKOUT])
    await world.linked(verified=False)
    assert await world.say("checkout crashes") == messages.LINK_GITHUB_FIRST
    assert world.llm.requests == []


async def test_no_repo_linked() -> None:
    world = World([CHECKOUT])
    assert await world.say("checkout crashes") == messages.NO_REPO_LINKED
    assert world.llm.requests == []


async def test_no_model_configured() -> None:
    world = World()
    world.services = replace(world.services, llm=None)
    await world.linked()
    assert await world.say("checkout crashes") == messages.LLM_NOT_CONFIGURED


async def test_the_model_sees_the_team_recent_messages_and_the_replied_one() -> None:
    world = World([NotARequest()])
    await world.linked()
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    earlier = make_message(
        "the checkout crashes on Android 13",
        message_id="5",
        sent_at=datetime.now(UTC) - timedelta(minutes=3),
    )
    then = make_message("and only when paying by card", message_id="6", sent_at=datetime.now(UTC))
    await world.buffer.remember(workspace.id, earlier)
    await world.buffer.remember(workspace.id, then)
    await world.say("make an issue from this", reply_to_message_id="5")
    [request] = world.llm.requests
    assert 'kind="team_members"' in request.message
    assert "Tekeu Franck (@franck_tg), GitHub @tekeufranck681" in request.message
    assert 'kind="recent_messages"' in request.message
    assert 'kind="replied_message"' in request.message
    assert request.message.count("the checkout crashes on Android 13") == 1  # not repeated
    assert "and only when paying by card" in request.message
    last_block = request.message.split("\n\n")[-1]
    assert 'kind="chat_message"' in last_block
    assert "make an issue from this" in last_block


async def test_no_recent_messages_when_the_team_turned_context_off() -> None:
    world = World([NotARequest()])
    await world.linked()
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.buffer.remember(workspace.id, make_message("secret plans", message_id="5"))
    await world.workspaces.set_keeps_context(workspace.id, keep=False)
    await world.say("hi")
    assert "secret plans" not in world.llm.requests[0].message


async def test_without_github_configured() -> None:
    messaging = FakeMessaging()
    await handle_inbound(make_message("hello", mentions_bot=True), messaging, None)
    assert [text for _, text, _ in messaging.sent] == [messages.GITHUB_NOT_CONFIGURED]


def test_help_explains_plain_language() -> None:
    assert "{bot} checkout crashes" in messages.HELP
    assert "Soon" not in messages.HELP
    assert not hasattr(messages, "NOT_UNDERSTOOD_YET")


async def test_no_read_access_stops_before_the_model_sees_anything() -> None:
    world = World([CHECKOUT])
    await world.linked()
    world.codehost.permissions[(SANDBOX.key, "tekeufranck681")] = RepoPermission.NONE
    reply = await world.say("list the open issues")
    assert "read" in reply
    assert world.llm.requests == []
    assert [(e.action, e.error) for e in world.audit.entries] == [("view", "no_repo_access")]


async def test_github_trouble_is_said_and_raised_for_the_logs() -> None:
    from chirpo.core.errors import CodeHostUnavailableError

    world = World([CHECKOUT])
    await world.linked()

    async def down(*args: object, **kwargs: object) -> list[object]:
        raise CodeHostUnavailableError("GitHub 502")

    world.codehost.list_issues = down  # type: ignore[method-assign]
    with pytest.raises(CodeHostUnavailableError):
        await world.say("checkout crashes")
    assert world.messaging.sent[-1][1] == messages.GITHUB_TROUBLE


async def test_typing_is_kept_up_while_the_model_thinks(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from chirpo.core import interpreter

    monkeypatch.setattr(interpreter, "TYPING_REFRESH_SECONDS", 0.01)
    world = World([NotARequest()])
    world.llm.delay = 0.06
    await world.linked()
    await world.say("thanks")
    shown = len(world.messaging.typing)
    assert shown >= 3  # refreshed while waiting, not just once
    await asyncio.sleep(0.05)
    assert len(world.messaging.typing) == shown  # and stopped once the reply was sent


# ------------------------------------------------------------------ reads run now (D47)


async def _issues(world: World) -> None:
    world.codehost.users |= {"tekeufranck681", "annie-gh"}
    for title, people, labels in [
        ("Checkout crashes on Android 13", ("tekeufranck681",), ("bug",)),
        ("Add dark mode", ("annie-gh",), ()),
        ("Export to CSV", ("tekeufranck681", "annie-gh"), ()),
    ]:
        await world.codehost.create_issue(
            SANDBOX, NewIssue(title=title, assignees=people, labels=labels)
        )


async def test_my_issues_are_listed_for_real() -> None:
    world = World([MyIssues()])
    await world.linked()
    await _issues(world)
    assert await world.say("what do I have on my plate?") == "\n".join(
        [
            messages.ISSUES_TITLE.format(
                state="Open", repo=SANDBOX.full_name, filters=messages.FILTER_YOURS
            ),
            "• #1 Checkout crashes on Android 13",
            "• #3 Export to CSV (also annie-gh)",
        ]
    )
    assert [(e.action, e.result) for e in world.audit.entries] == [("view", AuditResult.EXECUTED)]


async def test_someone_elses_issues_with_a_label() -> None:
    world = World([ListIssues(assignee="Franck", labels=("bug",))])
    await world.linked()
    await _issues(world)
    reply = await world.say("what bugs does Franck have?")
    assert reply.splitlines() == [
        messages.ISSUES_TITLE.format(
            state="Open",
            repo=SANDBOX.full_name,
            filters=messages.DRY_FILTER_ASSIGNEE.format(login="tekeufranck681")
            + messages.DRY_FILTER_LABELS.format(labels="bug"),
        ),
        "• #1 Checkout crashes on Android 13 (tekeufranck681)",
    ]


async def test_nothing_found() -> None:
    world = World([MyIssues()])
    await world.linked()
    assert await world.say("my issues?") == messages.ISSUES_NONE.format(
        state="open", repo=SANDBOX.full_name, filters=messages.FILTER_YOURS
    )


async def test_a_read_next_to_a_change_shows_both() -> None:
    world = World([MyIssues(), CHECKOUT])
    await world.linked()
    await _issues(world)
    reply = await world.say("what's mine, and log the checkout crash for Franck")
    parts = reply.split("\n\n")
    assert parts[0].startswith(messages.ISSUES_TITLE.split(" {")[0].format(state="Open"))
    assert parts[1].startswith(messages.DRY_RUN_HEADER)
    assert world.codehost.issues.keys() == {(SANDBOX.key, 1), (SANDBOX.key, 2), (SANDBOX.key, 3)}


async def test_the_context_is_gathered_at_the_same_time() -> None:
    import asyncio

    world = World([CHECKOUT])
    await world.linked()
    events: list[str] = []
    list_issues, members_all = world.codehost.list_issues, world.members.all

    async def slow_issues(*args: object, **kwargs: object) -> object:
        events.append("issues start")
        await asyncio.sleep(0.01)
        events.append("issues end")
        return await list_issues(*args, **kwargs)  # type: ignore[arg-type]

    async def members(*args: object, **kwargs: object) -> object:
        events.append("members")
        return await members_all(*args, **kwargs)  # type: ignore[arg-type]

    world.codehost.list_issues = slow_issues  # type: ignore[method-assign]
    world.members.all = members  # type: ignore[method-assign]
    await world.say("checkout crashes")
    assert events.index("members") < events.index("issues end")


async def test_each_step_is_timed_in_the_logs_without_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World([CHECKOUT])
    await world.linked()
    with caplog.at_level("INFO", logger="chirpo.core.interpreter"):
        await world.say("checkout crashes on Android 13")
    [line] = [r.getMessage() for r in caplog.records if r.name == "chirpo.core.interpreter"]
    for step in ("context_ms=", "model_ms=", "resolve_ms=", "reply_ms=", "total_ms="):
        assert step in line
    assert "checkout" not in line.casefold()


async def test_a_question_is_timed_too(caplog: pytest.LogCaptureFixture) -> None:
    world = World([AskClarifyingQuestion(question="Which issue?")])
    await world.linked()
    with caplog.at_level("INFO", logger="chirpo.core.interpreter"):
        await world.say("move it to done")
    [line] = [r.getMessage() for r in caplog.records if r.name == "chirpo.core.interpreter"]
    assert "model_ms=" in line and "total_ms=" in line


async def test_a_github_failure_stops_the_other_lookups() -> None:
    import asyncio

    from chirpo.core.errors import CodeHostUnavailableError

    world = World([CHECKOUT])
    await world.linked()
    finished: list[str] = []
    members_all = world.members.all

    async def down(*args: object, **kwargs: object) -> list[object]:
        raise CodeHostUnavailableError("GitHub 502")

    async def slow_members(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0.05)
        finished.append("members")
        return await members_all(*args, **kwargs)  # type: ignore[arg-type]

    world.codehost.list_issues = down  # type: ignore[method-assign]
    world.members.all = slow_members  # type: ignore[method-assign]
    with pytest.raises(CodeHostUnavailableError):
        await world.say("checkout crashes")
    await asyncio.sleep(0.1)
    assert finished == []
    assert world.messaging.sent[-1][1] == messages.GITHUB_TROUBLE


NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def limited(world: "World", *, member: int = 0, team: int = 0, tokens: int = 0) -> FakeCounters:
    counters = FakeCounters()
    limits = Limits(member, team, tokens, 0)
    rates = RateLimiter(counters, limits, now=lambda: NOON)
    world.services = replace(world.services, limits=rates)
    return counters


async def test_a_person_over_the_limit_is_told_once_and_the_model_is_not_called() -> None:
    world = World([CHECKOUT], [CHECKOUT])
    await world.linked()
    limited(world, member=2)
    await world.say("checkout crashes on Android 13")
    await world.say("checkout crashes on Android 13")
    reply = await world.say("checkout crashes on Android 13")
    assert reply == messages.RATE_LIMITED_MEMBER.format(wait="1 hour")
    assert len(world.llm.requests) == 2
    assert await world.say("checkout crashes on Android 13") == ""  # not told again


async def test_the_team_limit_applies_to_everyone() -> None:
    world = World([CHECKOUT], [CHECKOUT])
    await world.linked()
    world.codehost.permissions[(SANDBOX.key, "annie-gh")] = RepoPermission.READ
    limited(world, member=5, team=2)
    await world.say("checkout crashes")
    await world.say("checkout crashes", sender=ANNIE)
    reply = await world.say("checkout crashes")
    assert reply == messages.RATE_LIMITED_TEAM.format(wait="1 hour")
    assert len(world.llm.requests) == 2


async def test_a_refused_request_reads_nothing_from_github() -> None:
    world = World([CHECKOUT])
    await world.linked()
    limited(world, member=1)
    reads: list[object] = []
    list_issues = world.codehost.list_issues

    async def counted(*args: object, **kwargs: object) -> object:
        reads.append(args)
        return await list_issues(*args, **kwargs)  # type: ignore[arg-type]

    world.codehost.list_issues = counted  # type: ignore[method-assign]
    await world.say("checkout crashes")
    assert len(reads) == 1
    await world.say("checkout crashes")
    assert len(reads) == 1
    assert len(world.llm.requests) == 1


async def test_someone_who_hasnt_linked_github_uses_no_allowance() -> None:
    world = World()
    await world.linked(verified=False)
    counters = limited(world, member=1)
    assert await world.say("checkout crashes") == messages.LINK_GITHUB_FIRST
    assert counters.values == {}


async def test_tokens_count_toward_the_daily_budget() -> None:
    world = World([CHECKOUT], [CHECKOUT])
    world.llm = FakeLLM([CHECKOUT], [CHECKOUT], tokens=(5_800, 200))
    world.services = replace(world.services, llm=world.llm)
    await world.linked()
    limited(world, tokens=10_000)
    await world.say("checkout crashes")  # 6,000 used
    await world.say("checkout crashes")  # 12,000 used: the model was still asked
    reply = await world.say("checkout crashes")
    assert reply == messages.RATE_LIMITED_BUDGET.format(wait="12 hours")
    assert len(world.llm.requests) == 2


async def test_commands_are_not_limited() -> None:
    world = World()
    await world.linked()
    limited(world, member=1, team=1)
    for _ in range(3):
        assert await world.say("/ping") == messages.PONG


async def test_a_failed_refusal_reply_does_not_break_the_request() -> None:
    world = World([CHECKOUT], [CHECKOUT])
    await world.linked()
    limited(world, member=1)
    await world.say("checkout crashes")
    world.messaging.unreachable_chats = {CHAT.chat_id}
    await world.say("checkout crashes")  # the refusal can't be delivered: no error
    assert len(world.llm.requests) == 1


async def test_a_photo_without_text_is_not_a_request() -> None:
    world = World([CHECKOUT])
    await world.linked()
    photo = Attachment(kind=AttachmentKind.IMAGE, file_id="P1", mime_type="image/jpeg")
    assert await world.say("", attachments=(photo,)) == ""
    assert world.llm.requests == []
    assert world.messaging.typing == []


async def test_a_photo_with_a_caption_is_handled_like_any_request() -> None:
    world = World([CHECKOUT])
    await world.linked()
    photo = Attachment(kind=AttachmentKind.IMAGE, file_id="P1", mime_type="image/jpeg")
    reply = await world.say("checkout crashes on Android 13", attachments=(photo,))
    assert reply.startswith(messages.DRY_RUN_HEADER)
    assert len(world.llm.requests) == 1
