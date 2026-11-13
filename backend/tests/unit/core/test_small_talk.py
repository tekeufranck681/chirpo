"""Small talk (M6.11): thanks, compliments and greetings get a natural reply from the model call
that would have said `not_a_request`. The reply is model text, so it is cleaned like anything we
post; a request mixed in still gets its card; `off` keeps the neutral fixed reply."""

from chirpo.core import messages
from chirpo.core.actions import CreateIssue, IssueRef, NotARequest, SetStatus, SmallTalk
from chirpo.core.outbound import ZWSP
from chirpo.core.pending import PendingStatus
from tests.fakes import CHAT
from tests.unit.core.test_interpreter import FRANCK, SANDBOX, World, _issues


async def set_personality(world: World, level: str) -> None:
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.workspaces.set_personality(workspace.id, level)  # type: ignore[arg-type]


def tool_names(world: World) -> set[str]:
    return {tool.name for tool in world.llm.requests[-1].tools}


def fixed_text(world: World) -> str:
    return messages.NOT_A_REQUEST.format(bot=world.messaging.bot_mention())


async def test_thanks_gets_the_models_own_reply() -> None:
    world = World([SmallTalk(reply="You're welcome, Franck!")])
    await world.linked()
    assert await world.say("thanks, you're a genius") == "You're welcome, Franck!"
    assert world.codehost.issues == {}


async def test_an_at_mention_in_the_reply_is_broken_so_nobody_is_notified() -> None:
    world = World([SmallTalk(reply="Glad to help @annie-gh and @franck_tg!")])
    await world.linked()
    reply = await world.say("thanks")
    assert f"@{ZWSP}annie-gh" in reply and f"@{ZWSP}franck_tg" in reply


async def test_a_long_reply_is_cut() -> None:
    world = World([SmallTalk(reply="Thanks! " * 45)])
    await world.linked()
    assert len(await world.say("thanks")) <= 280


async def test_a_blank_reply_falls_back_to_the_fixed_text() -> None:
    world = World([SmallTalk(reply="   ")])
    await world.linked()
    assert await world.say("thanks") == fixed_text(world)


async def test_a_reply_with_invisible_characters_or_too_long_is_accepted_and_cleaned() -> None:
    """A strict parse would reject the whole answer, request included (one bad call voids all)."""
    world = World([SmallTalk(reply="Glad\u200b to help\u202e! " + "x" * 600)])
    await world.linked()
    reply = await world.say("thanks")
    assert reply.startswith("Glad to help! xxx") and len(reply) <= 280


async def test_an_odd_small_talk_next_to_a_request_still_gives_the_card() -> None:
    world = World([SmallTalk(reply="\u200b" + "y" * 900), CreateIssue(title="Export to CSV")])
    await world.linked()
    assert (await world.say("thanks! log the CSV export")).startswith(messages.DRY_RUN_HEADER)


async def test_a_reply_with_a_link_is_not_posted() -> None:
    for reply in ("Visit http://evil.example now", "see www.evil.example", "join t.me/evil_group"):
        world = World([SmallTalk(reply=reply)])
        await world.linked()
        assert await world.say("thanks") == fixed_text(world), reply


async def test_a_bot_command_in_the_reply_is_not_tappable() -> None:
    world = World([SmallTalk(reply="Try /unlink acme/api or /role @x admin")])
    await world.linked()
    reply = await world.say("thanks")
    assert "/unlink" not in reply and f"/{ZWSP}unlink" in reply


async def test_invisible_format_characters_can_not_hide_a_mention() -> None:
    world = World([SmallTalk(reply="hi @\u2060annie-gh and @\U000e0061bc")])
    await world.linked()
    reply = await world.say("thanks")
    assert "\u2060" not in reply and "\U000e0061" not in reply
    assert f"@{ZWSP}annie-gh" in reply


async def test_more_mention_shapes_are_broken() -> None:
    world = World([SmallTalk(reply="(@annie) @a/team @everyone")])
    await world.linked()
    reply = await world.say("thanks")
    assert f"(@{ZWSP}annie)" in reply and f"@{ZWSP}a/team" in reply and f"@{ZWSP}everyone" in reply
    world = World([SmallTalk(reply="@annie-gh thanks")])
    await world.linked()
    assert (await world.say("thanks")).startswith(f"@{ZWSP}annie-gh")


async def test_the_reply_is_a_single_line() -> None:
    world = World([SmallTalk(reply="Happy to help.\n\nAnything else?")])
    await world.linked()
    assert await world.say("thanks") == "Happy to help. Anything else?"


async def test_small_talk_next_to_a_request_is_dropped_and_the_card_is_unchanged() -> None:
    world = World([SmallTalk(reply="You're welcome!"), CreateIssue(title="Export to CSV")])
    await world.linked()
    card = await world.say("thanks! now log the CSV export")
    assert card.startswith(messages.DRY_RUN_HEADER)
    assert "welcome" not in card
    assert len(world.messaging.sent) == 1


async def test_small_talk_next_to_a_status_change_does_not_stop_it() -> None:
    world = World(
        [SmallTalk(reply="Glad it helped!"), SetStatus(issue=IssueRef(number=2), status="review")]
    )
    await world.linked()
    await _issues(world)
    text = await world.say("thanks, now move #2 to review")
    assert "Glad it helped" not in text
    assert "status: review" in world.codehost.issues[(SANDBOX.key, 2)].labels


async def test_small_talk_and_not_a_request_together_still_reply_with_the_small_talk() -> None:
    world = World([NotARequest(), SmallTalk(reply="Anytime!")])
    await world.linked()
    assert await world.say("thanks") == "Anytime!"


async def test_a_thanks_to_a_waiting_card_still_points_to_the_buttons() -> None:
    world = World([CreateIssue(title="Export to CSV")], [SmallTalk(reply="Anytime!")])
    await world.linked()
    await world.say("log the CSV export")
    card = world.messaging.last_id
    text = await world.say(
        "looks good, thanks", FRANCK, reply_to_message_id=card, replies_to_bot=True
    )
    assert text == messages.USE_THE_BUTTONS
    [row] = world.services.pending.rows.values()  # type: ignore[attr-defined]
    assert row.status == PendingStatus.PROPOSED


# ----------------------------------------------------------------------------- personality


async def test_light_is_the_default_and_offers_the_tool_and_its_rules() -> None:
    world = World([SmallTalk(reply="Hi!")])
    await world.linked()
    await world.say("hello")
    assert "small_talk" in tool_names(world)
    assert "## Small talk" in world.llm.requests[-1].instructions
    assert "## Playful" not in world.llm.requests[-1].instructions


async def test_off_does_not_offer_the_tool_or_its_rules() -> None:
    world = World([NotARequest()])
    await world.linked()
    await set_personality(world, "off")
    reply = await world.say("thanks")
    assert "small_talk" not in tool_names(world)
    assert "small_talk" not in world.llm.requests[-1].instructions  # no name, no pointer to rules
    assert "## Small talk" not in world.llm.requests[-1].instructions
    assert reply == fixed_text(world)


async def test_off_ignores_a_small_talk_the_model_sends_anyway() -> None:
    world = World([SmallTalk(reply="You're welcome!")])
    await world.linked()
    await set_personality(world, "off")
    assert await world.say("thanks") == fixed_text(world)


async def test_playful_adds_its_own_rules() -> None:
    world = World([SmallTalk(reply="Always!")])
    await world.linked()
    await set_personality(world, "playful")
    await world.say("tell us a joke")
    instructions = world.llm.requests[-1].instructions
    assert "## Small talk" in instructions and "## Playful" in instructions
    assert "small_talk" in tool_names(world)


# ------------------------------------------------------------------------- prompt and tools


def test_the_prompt_sections_include_small_talk_and_playful_in_a_fixed_order() -> None:
    from chirpo.core.llm import SECTIONS, system_prompt

    assert SECTIONS == ("images", "proposal", "smalltalk", "playful")
    full = system_prompt(frozenset(SECTIONS))
    positions = [full.index(marker) for marker in ("## Small talk", "## Playful")]
    assert positions == sorted(positions)


def test_the_tool_list_in_the_prompt_leaves_out_small_talk_unless_its_rules_are_in() -> None:
    from chirpo.core.llm import system_prompt

    assert "- `small_talk`" not in system_prompt(frozenset())
    assert "- `small_talk`" in system_prompt(frozenset({"smalltalk"}))


def test_the_request_follows_the_personality() -> None:
    from chirpo.core.llm import interpret_request

    def offered(level: str) -> set[str]:
        return {t.name for t in interpret_request("thanks", "Franck", personality=level).tools}

    assert "small_talk" not in offered("off")
    assert "small_talk" in offered("light") and "small_talk" in offered("playful")
    playful = interpret_request("hi", "Franck", personality="playful").instructions
    light = interpret_request("hi", "Franck", personality="light").instructions
    assert "## Playful" in playful and "## Playful" not in light


def test_small_talk_is_text_only_and_cannot_act() -> None:
    from chirpo.core.actions import ACTIONS

    spec = ACTIONS["small_talk"]
    assert spec.permission is None and not spec.needs_confirmation  # nothing on the code host
    assert set(SmallTalk.model_fields) == {"reply"}


def test_the_base_prompt_never_tells_the_model_how_to_answer_thanks() -> None:
    """That belongs to the small talk section; the base example would contradict it."""
    from chirpo.core.llm import system_prompt

    base = system_prompt(frozenset())
    assert "thanks chirpo" not in base
    assert "thanks chirpo" in system_prompt(frozenset({"smalltalk"}))


def test_the_small_talk_rules_say_never_to_quote_the_chat() -> None:
    from chirpo.core.llm import system_prompt

    assert "Never repeat or quote" in system_prompt(frozenset({"smalltalk"}))


def test_an_unknown_stored_personality_reads_as_light() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from chirpo.db.repositories import _to_workspace

    for stored in ("grumpy", 5, None, ["off"]):
        row = SimpleNamespace(id=uuid4(), name="Shop", settings={"personality": stored})
        assert _to_workspace(row).personality == "light"  # type: ignore[arg-type]
    row = SimpleNamespace(id=uuid4(), name="Shop", settings={"personality": "off"})
    assert _to_workspace(row).personality == "off"  # type: ignore[arg-type]


async def test_issue_titles_offered_in_a_question_cannot_mention_anyone() -> None:
    from chirpo.core.actions import IssueRef
    from chirpo.core.domain import NewIssue

    world = World([SetStatus(issue=IssueRef(description="checkout"), status="done")])
    await world.linked()
    for title in ("Checkout @everyone broke", "Checkout total wrong @annie_n"):
        await world.codehost.create_issue(SANDBOX, NewIssue(title=title))
    text = await world.say("the checkout one is done")
    assert "@everyone" not in text and "@annie_n" not in text
    assert f"@{ZWSP}everyone" in text
