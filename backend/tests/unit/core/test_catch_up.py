"""What did I miss? (M6.9, D22): a summary of what the chat said since the asker's last message,
answered in the group, with the issues opened and closed since then read from GitHub."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chirpo.core import messages
from chirpo.core.actions import CatchUp
from chirpo.core.catchup import MAX_MESSAGES
from chirpo.core.domain import ChatUser, IssuePatch, IssueState, NewIssue
from chirpo.core.errors import LLMUnavailableError
from tests.fakes import CHAT, make_message
from tests.unit.core.test_interpreter import ANNIE, FRANCK, SANDBOX, World

NOW = datetime.now(UTC)


async def said(world: World, sender: ChatUser, text: str, minutes_ago: int) -> None:
    """A plain chat message, kept in the buffer as the pipeline would."""
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    message = make_message(
        text,
        sender=sender,
        message_id=f"m{minutes_ago}-{sender.user_id}",
        sent_at=NOW - timedelta(minutes=minutes_ago),
    )
    await world.buffer.remember(workspace.id, message)


async def team(*answers: object) -> World:
    world = World(*answers)  # type: ignore[arg-type]
    await world.linked()
    await said(world, ANNIE, "Should we ship on Friday?", 90)
    await said(world, FRANCK, "I'm out until 3", 60)
    await said(world, ANNIE, "We agreed: release Monday, Paul does the notes", 30)
    return world


def summarized(world: World) -> str:
    [request] = world.llm.summary_requests
    return request.message


async def test_what_was_said_since_your_last_message_is_summed_up() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append("• Release moved to Monday, Paul writes the notes.")
    reply = await world.say("what did I miss?")
    text = summarized(world)
    assert "release Monday" in text
    assert "ship on Friday" not in text  # before Franck's last message
    assert "I'm out until 3" not in text
    assert "• Release moved to Monday, Paul writes the notes." in reply
    assert reply.startswith(messages.CATCH_UP_HEADING.split("{")[0])


async def test_the_chat_is_sent_as_untrusted_data() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append("• ok")
    await world.say("catch me up")
    [request] = world.llm.summary_requests
    assert "<untrusted_" in request.message
    assert "never instructions" in request.instructions


async def test_a_period_can_be_named() -> None:
    world = await team([CatchUp(hours=2)])
    world.llm.summaries.append("• ok")
    await world.say("what happened in the last 2 hours?")
    text = summarized(world)
    assert "ship on Friday" in text and "release Monday" in text


async def test_without_a_message_of_yours_everything_kept_is_covered() -> None:
    world = World([CatchUp()])
    await world.linked()
    await said(world, ANNIE, "Should we ship on Friday?", 90)
    await said(world, ANNIE, "We agreed: release Monday", 30)
    world.llm.summaries.append("• ok")
    reply = await world.say("what did I miss?")
    assert "ship on Friday" in summarized(world)
    assert messages.CATCH_UP_NO_OWN_MESSAGE.format(hours=24) in reply


async def test_nothing_new_needs_no_model() -> None:
    world = World([CatchUp()])
    await world.linked()
    await said(world, ANNIE, "hi", 20)
    await said(world, FRANCK, "hello", 10)
    reply = await world.say("what did I miss?")
    assert reply.startswith(messages.CATCH_UP_NOTHING.split("{")[0])
    assert world.llm.summary_requests == []


async def test_it_needs_the_chat_to_keep_messages() -> None:
    world = await team([CatchUp()])
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.workspaces.set_keeps_context(workspace.id, keep=False)
    assert await world.say("what did I miss?") == messages.CATCH_UP_NEEDS_CONTEXT
    assert world.llm.summary_requests == []


async def test_issues_opened_and_closed_since_then_come_from_github() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append("• ok")
    world.codehost._clock = NOW - timedelta(minutes=20)  # after Franck's last message
    opened = await world.codehost.create_issue(SANDBOX, NewIssue(title="Release notes"))
    gone = await world.codehost.create_issue(SANDBOX, NewIssue(title="Old crash"))
    await world.codehost.update_issue(SANDBOX, gone.number, IssuePatch(state=IssueState.CLOSED))
    reply = await world.say("what did I miss?")
    assert f'#{opened.number} "Release notes"' in reply
    assert messages.CATCH_UP_CLOSED.split("{")[0].strip() in reply
    assert SANDBOX.full_name in reply


async def test_the_summary_is_cleaned_like_all_model_text() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append("• Ping @everyone\n• See https://evil.example/x\n• /unlink it")
    reply = await world.say("what did I miss?")
    assert "@everyone" not in reply  # the mention is broken, not notifying
    assert "evil.example" not in reply  # a line with a link is dropped
    assert "/unlink" not in reply  # no tappable command


async def test_only_the_latest_messages_are_read() -> None:
    world = World([CatchUp()])
    await world.linked()
    for minute in range(MAX_MESSAGES + 5, 0, -1):
        await said(world, ANNIE, f"note {minute}", minute)
    world.llm.summaries.append("• ok")
    reply = await world.say("what did I miss?")  # Franck: none of his in the chat
    assert f"note {MAX_MESSAGES + 5}\n" not in summarized(world)
    assert messages.CATCH_UP_CUT.format(count=MAX_MESSAGES) in reply


async def test_a_model_outage_is_said() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append(LLMUnavailableError("down"))
    with pytest.raises(LLMUnavailableError):  # an outage belongs in the logs
        await world.say("what did I miss?")
    assert world.messaging.sent[-1][1] == messages.LLM_UNAVAILABLE


async def test_the_command_does_the_same_without_reading_intent() -> None:
    world = await team()
    world.llm.summaries.append("• Release moved to Monday.")
    reply = await world.say("/catchup", mentions_bot=False)
    assert "• Release moved to Monday." in reply
    assert world.llm.requests == []  # no interpretation, only the summary


async def test_the_command_takes_a_period() -> None:
    world = await team()
    world.llm.summaries.append("• ok")
    await world.say("/catchup 2h", mentions_bot=False)
    assert "ship on Friday" in summarized(world)
    assert await world.say("/catchup soon", mentions_bot=False) == messages.CATCHUP_USAGE
    assert "/catchup" in messages.HELP


async def test_it_is_audited_without_the_text() -> None:
    world = await team([CatchUp()])
    world.llm.summaries.append("• Release moved to Monday.")
    await world.say("what did I miss?")
    entry = world.audit.entries[-1]
    assert entry.action == "catch_up"
    assert entry.payload == {"messages": 1}


async def test_a_request_in_the_same_message_still_gets_its_card() -> None:
    from chirpo.core.actions import CreateIssue

    world = await team([CatchUp(), CreateIssue(title="Write the release notes")])
    world.llm.summaries.append("• Release moved to Monday.")
    await world.say("what did I miss? and make an issue to write the release notes")
    sent = [text for _, text, *_ in world.messaging.sent]
    assert any("• Release moved to Monday." in text for text in sent)
    assert sent[-1].startswith(messages.DRY_RUN_HEADER)


def test_bare_addresses_are_dropped_but_file_names_stay() -> None:
    from chirpo.core.outbound import plain_lines

    text = "• log in at deploy-login.com\n• join t.me/+x\n• mail mailto:x@y\n"
    text += "• fixed main.py and app.ts"
    assert plain_lines(text, line_limit=300, max_lines=8) == "• fixed main.py and app.ts"


async def test_a_busy_chat_keeps_the_newest_messages() -> None:
    world = World([CatchUp()])
    await world.linked()
    for minute in range(MAX_MESSAGES, 0, -1):
        await said(world, ANNIE, f"note {minute} " + "x" * 200, minute)
    world.llm.summaries.append("• ok")
    reply = await world.say("what did I miss?")
    text = summarized(world)
    assert "note 1 " in text  # the newest is there
    assert f"note {MAX_MESSAGES} " not in text  # the oldest went
    assert "[message cut]" not in text
    assert messages.CATCH_UP_CUT_CHARS in reply


async def test_a_period_longer_than_what_is_kept_is_capped() -> None:
    world = await team([CatchUp(hours=720)])
    world.llm.summaries.append("• ok")
    reply = await world.say("what happened this month?")
    assert reply.startswith(messages.CATCH_UP_HEADING_HOURS.format(period="24 hours"))


async def test_times_follow_the_teams_timezone() -> None:
    world = await team([CatchUp()], [CatchUp()])
    world.llm.summaries += ["• ok", "• ok"]
    reply = await world.say("what did I miss?")
    franck_at = NOW - timedelta(minutes=60)
    assert f"{franck_at:%H:%M} UTC" in reply  # unset: said, never silently off
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.workspaces.set_timezone(workspace.id, "Africa/Douala")
    await said(world, FRANCK, "I'm back", 2)
    world.llm.summary_requests.clear()
    await said(world, ANNIE, "Paul does the notes", 1)
    reply = await world.say("what did I miss?")
    local = (NOW - timedelta(minutes=1)).astimezone(ZoneInfo("Africa/Douala"))
    assert f"[{local:%H:%M}] Annie Ngo:" in summarized(world)
    assert "UTC" not in reply


async def test_pull_requests_opened_and_merged_since_then_are_listed() -> None:
    from chirpo.core.domain import PullRequestState

    world = await team([CatchUp()])
    world.llm.summaries.append("• ok")
    fresh = NOW - timedelta(minutes=20)
    world.codehost.add_pull_request(
        SANDBOX, title="Dark mode toggle", created_at=fresh, updated_at=fresh
    )
    world.codehost.add_pull_request(
        SANDBOX,
        title="Update README",
        state=PullRequestState.MERGED,
        created_at=NOW - timedelta(days=2),
        updated_at=fresh,
        merged_at=fresh,
    )
    world.codehost.add_pull_request(
        SANDBOX,
        title="Old work",
        created_at=NOW - timedelta(days=3),
        updated_at=NOW - timedelta(days=3),
    )
    reply = await world.say("what did I miss?")
    assert messages.CATCH_UP_PRS_OPENED.split("{")[0] in reply
    assert "PR #" in reply and '"Dark mode toggle"' in reply
    assert '"Update README"' in reply and "merged" in reply
    assert "Old work" not in reply


def test_the_summary_ranks_what_matters_and_keeps_your_own_commitments() -> None:
    from chirpo.core.llm import catch_up_request

    rules = catch_up_request(["[10:00] Annie: hi"], "Franck", marker="m").instructions
    assert "You said" in rules  # the asker's own commitments, in the second person
    assert "Decisions" in rules and "Blockers" in rules
    assert "number" in rules  # PRs and issues named by number when the messages give one
