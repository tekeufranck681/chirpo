"""Looking back at the most recent image of a chat, only when the model asks (D52).

"Make an issue from this" often comes without a reply, right after somebody's screenshot. Always
sending that screenshot would upload photos that have nothing to do with the request, so the
model first sees only a text marker. If the message refers to the image it calls
`look_at_recent_image`; only then is the picture fetched and the question asked once more.
"""

import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from chirpo.core.actions import LookAtRecentImage
from chirpo.core.domain import Attachment, RecentMessage
from chirpo.core.images import can_read
from chirpo.core.llm import Interpretation, RequestImage, interpret_request
from chirpo.core.ports import LLMProvider
from chirpo.core.vision import Seen
from chirpo.core.workspaces import DEFAULT_PERSONALITY, Personality

RECENT_IMAGE_MINUTES = 10

Fetch = Callable[[Attachment], Awaitable[Seen]]
Charge = Callable[[Interpretation], Awaitable[None]]  # records what a model call used


@dataclass(frozen=True, slots=True)
class RecentImage:
    message_id: str
    attachment: Attachment


@dataclass(frozen=True, slots=True)
class Asked:
    interpretation: Interpretation  # both calls' tokens added up
    notes: list[str]
    looked: bool = False  # the image was fetched and shown
    failed: bool = False  # it was asked for and could not be fetched: nothing to answer


def recent_image(recent: Sequence[RecentMessage], *, now: datetime) -> RecentImage | None:
    """The newest message from the last few minutes with an image chirpo can read."""
    window = timedelta(minutes=RECENT_IMAGE_MINUTES)
    # Newest first; messages sent in the same second keep the buffer's order, so the last wins.
    newest_first = sorted(
        enumerate(recent), key=lambda pair: (pair[1].sent_at, pair[0]), reverse=True
    )
    for _, message in newest_first:
        if now - message.sent_at > window:
            return None  # everything after this is older
        attachment = next((a for a in message.attachments if can_read(a)), None)
        if attachment is not None:
            return RecentImage(message.message_id, attachment)
    return None


async def interpret(
    llm: LLMProvider,
    *,
    text: str,
    sender: str,
    context: Sequence[tuple[str, str]],
    images: Sequence[RequestImage],
    candidate: Attachment | None,
    fetch: Fetch,
    charge: Charge | None = None,
    personality: Personality = DEFAULT_PERSONALITY,
) -> Asked:
    """The tool is offered only when `candidate` exists, and only once: the second call can't ask
    again. Whatever else came with a request to look is ignored; the second answer decides.
    `charge` hears about each call's usage as its answer arrives, so a later failure can't make
    a call free."""
    marker = secrets.token_hex(8)  # the same for both calls: the question must not change
    first = await llm.interpret(
        interpret_request(
            text,
            sender,
            context=context,
            images=images,
            look=candidate is not None,
            marker=marker,
            personality=personality,
        )
    )
    if charge is not None:
        await charge(first)
    if candidate is None or not _asks_to_look(first):
        return Asked(_without_looks(first), [])
    seen = await fetch(candidate)
    if not seen.images:
        return Asked(_without_looks(first), list(seen.notes), failed=True)
    second = await llm.interpret(
        interpret_request(
            text,
            sender,
            context=context,
            images=[*images, *seen.images],
            marker=marker,
            personality=personality,
        )
    )
    if charge is not None:
        await charge(second)
    answer = replace(
        _without_looks(second),
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
    )
    return Asked(answer, list(seen.notes), looked=True)


def _asks_to_look(answer: Interpretation) -> bool:
    return any(isinstance(p, LookAtRecentImage) for p in answer.proposals)


def _without_looks(answer: Interpretation) -> Interpretation:
    kept = tuple(p for p in answer.proposals if not isinstance(p, LookAtRecentImage))
    return replace(answer, proposals=kept)
