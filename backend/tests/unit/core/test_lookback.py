from datetime import UTC, datetime, timedelta

import pytest

from chirpo.core import messages
from chirpo.core.actions import CreateIssue, LookAtRecentImage, NotARequest
from chirpo.core.domain import Attachment, AttachmentKind, ChatUser, RecentMessage
from chirpo.core.errors import LLMUnavailableError
from chirpo.core.llm import IMAGE_RECENT, Interpretation, RequestImage
from chirpo.core.lookback import RECENT_IMAGE_MINUTES, Asked, Charge, Fetch, interpret, recent_image
from chirpo.core.vision import Seen
from tests.fakes import FakeLLM

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ANNIE = ChatUser(platform="fake", user_id="77", display_name="Annie")
PNG = RequestImage(label=IMAGE_RECENT, mime_type="image/png", data=b"\x89PNG-bytes")
CHECKOUT = CreateIssue(title="Checkout crashes", labels=("bug",))


def photo(
    file_id: str = "P1", mime: str | None = "image/jpeg", size: int | None = 900
) -> Attachment:
    return Attachment(kind=AttachmentKind.IMAGE, file_id=file_id, mime_type=mime, size=size)


def said(
    message_id: str, minutes_ago: float, *attachments: Attachment, text: str = ""
) -> RecentMessage:
    return RecentMessage(
        message_id=message_id,
        sender=ANNIE,
        text=text,
        sent_at=NOW - timedelta(minutes=minutes_ago),
        attachments=attachments,
    )


# ------------------------------------------------------------------ finding the image


def test_the_most_recent_image_within_ten_minutes_is_found() -> None:
    older, newer = said("1", 8, photo("OLD")), said("2", 3, photo("NEW"))
    found = recent_image([older, newer], now=NOW)
    assert found is not None
    assert (found.message_id, found.attachment.file_id) == ("2", "NEW")


def test_images_older_than_the_window_are_not_offered() -> None:
    assert recent_image([said("1", RECENT_IMAGE_MINUTES + 1, photo())], now=NOW) is None
    assert recent_image([said("1", RECENT_IMAGE_MINUTES - 0.5, photo())], now=NOW) is not None


def test_only_images_chirpo_can_read_are_offered() -> None:
    svg = photo("S", "image/svg+xml")
    huge = photo("H", size=9_000_000)
    pdf = Attachment(kind=AttachmentKind.FILE, file_id="D", mime_type="application/pdf", size=10)
    voice = Attachment(kind=AttachmentKind.VOICE, file_id="V", mime_type="audio/ogg", size=10)
    assert recent_image([said("1", 1, svg, huge, pdf, voice)], now=NOW) is None


def test_the_newest_readable_one_wins_over_a_newer_unreadable_one() -> None:
    svg_message = said("2", 1, photo("S", "image/svg+xml"))
    found = recent_image([said("1", 5, photo("GOOD")), svg_message], now=NOW)
    assert found is not None
    assert found.attachment.file_id == "GOOD"


def test_a_message_with_several_images_offers_the_first_readable_one() -> None:
    found = recent_image([said("1", 2, photo("A", "image/gif"), photo("B"), photo("C"))], now=NOW)
    assert found is not None
    assert found.attachment.file_id == "B"


def test_no_recent_messages_means_no_image() -> None:
    assert recent_image([], now=NOW) is None
    assert recent_image([said("1", 1)], now=NOW) is None


# ------------------------------------------------------------------ asking the model


async def fetch_ok(attachment: Attachment) -> Seen:
    return Seen((PNG,), [])


SHOT = photo()


async def ask(
    llm: FakeLLM,
    *,
    candidate: Attachment | None = SHOT,
    fetch: Fetch = fetch_ok,
    images: tuple[RequestImage, ...] = (),
    charge: Charge | None = None,
) -> Asked:
    return await interpret(
        llm,
        text="log that as a bug",
        sender="Franck",
        context=[("recent_messages", "[12:00] Annie: [image, can be looked at]")],
        images=images,
        candidate=candidate,
        fetch=fetch,
        charge=charge,
    )


async def test_an_answer_that_doesnt_ask_to_look_is_the_answer() -> None:
    llm = FakeLLM([CHECKOUT])
    asked = await ask(llm)
    assert asked.interpretation.proposals == (CHECKOUT,)
    assert (asked.looked, asked.failed, asked.notes) == (False, False, [])
    assert len(llm.requests) == 1


async def test_asking_to_look_shows_the_image_and_asks_once_more() -> None:
    llm = FakeLLM([LookAtRecentImage()], [CHECKOUT], tokens=(1000, 50))
    asked = await ask(llm)
    assert asked.looked
    assert asked.interpretation.proposals == (CHECKOUT,)
    first, second = llm.requests
    assert first.images == () and second.images == (PNG,)
    assert second.message == first.message  # the same question, now with the picture
    assert (asked.interpretation.input_tokens, asked.interpretation.output_tokens) == (2000, 100)


async def test_the_tool_is_only_offered_when_there_is_an_image_to_look_at() -> None:
    names = lambda request: {tool.name for tool in request.tools}  # noqa: E731
    with_image, without = FakeLLM([CHECKOUT]), FakeLLM([CHECKOUT])
    await ask(with_image)
    await ask(without, candidate=None)
    assert "look_at_recent_image" in names(with_image.requests[0])
    assert "look_at_recent_image" not in names(without.requests[0])


async def test_there_is_no_second_chance_to_look() -> None:
    llm = FakeLLM([LookAtRecentImage()], [LookAtRecentImage(), CHECKOUT])
    asked = await ask(llm)
    assert "look_at_recent_image" not in {tool.name for tool in llm.requests[1].tools}
    assert asked.interpretation.proposals == (CHECKOUT,)  # the stray call is dropped


async def test_whatever_else_came_with_the_request_to_look_is_ignored() -> None:
    llm = FakeLLM([CheckoutThenLook := LookAtRecentImage(), CHECKOUT], [NotARequest()])
    asked = await ask(llm)
    assert asked.interpretation.proposals == (NotARequest(),)
    assert CheckoutThenLook  # silence the unused name


async def test_a_look_without_an_image_to_look_at_is_dropped() -> None:
    llm = FakeLLM([LookAtRecentImage(), CHECKOUT])
    asked = await ask(llm, candidate=None)
    assert asked.interpretation.proposals == (CHECKOUT,)
    assert (asked.looked, len(llm.requests)) == (False, 1)


async def test_when_the_image_cant_be_fetched_there_is_no_second_call() -> None:
    async def gone(attachment: Attachment) -> Seen:
        return Seen((), [messages.IMAGE_UNAVAILABLE])

    llm = FakeLLM([LookAtRecentImage()], [CHECKOUT])
    asked = await ask(llm, fetch=gone)
    assert (asked.failed, asked.notes) == (True, [messages.IMAGE_UNAVAILABLE])
    assert len(llm.requests) == 1


async def test_images_sent_with_the_message_stay_in_both_requests() -> None:
    own = RequestImage(label="own", mime_type="image/png", data=b"x")
    llm = FakeLLM([LookAtRecentImage()], [CHECKOUT])
    await ask(llm, images=(own,))
    assert llm.requests[0].images == (own,)
    assert llm.requests[1].images == (own, PNG)


async def test_model_errors_pass_through_from_either_call() -> None:
    with pytest.raises(LLMUnavailableError):
        await ask(FakeLLM(LLMUnavailableError("down")))
    with pytest.raises(LLMUnavailableError):
        await ask(FakeLLM([LookAtRecentImage()], LLMUnavailableError("down")))


class Spent:
    def __init__(self) -> None:
        self.tokens: list[int] = []

    async def __call__(self, answer: Interpretation) -> None:
        self.tokens.append(answer.input_tokens + answer.output_tokens)


async def test_every_call_is_charged_as_its_answer_arrives() -> None:
    spent = Spent()
    await ask(FakeLLM([LookAtRecentImage()], [CHECKOUT], tokens=(1000, 50)), charge=spent)
    assert spent.tokens == [1050, 1050]
    once = Spent()
    await ask(FakeLLM([CHECKOUT], tokens=(900, 10)), charge=once)
    assert once.tokens == [910]


async def test_the_first_call_is_charged_even_when_the_image_cant_be_fetched() -> None:
    async def gone(attachment: Attachment) -> Seen:
        return Seen((), [messages.IMAGE_UNAVAILABLE])

    spent = Spent()
    await ask(FakeLLM([LookAtRecentImage()], tokens=(1000, 50)), fetch=gone, charge=spent)
    assert spent.tokens == [1050]


async def test_the_first_call_is_charged_even_when_the_second_fails() -> None:
    spent = Spent()
    llm = FakeLLM([LookAtRecentImage()], LLMUnavailableError("down"), tokens=(1000, 50))
    with pytest.raises(LLMUnavailableError):
        await ask(llm, charge=spent)
    assert spent.tokens == [1050]


def test_photos_sent_in_the_same_second_pick_the_last_one() -> None:
    first, second = said("1", 3, photo("FIRST")), said("2", 3, photo("SECOND"))
    found = recent_image([first, second], now=NOW)  # the buffer lists them oldest first
    assert found is not None
    assert found.attachment.file_id == "SECOND"


def test_exactly_ten_minutes_ago_still_counts() -> None:
    assert recent_image([said("1", RECENT_IMAGE_MINUTES, photo())], now=NOW) is not None
