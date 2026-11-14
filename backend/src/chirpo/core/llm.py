"""What the core sends an LLM and what comes back (D45).

The system prompt is `prompts/base.md` plus the sections a request can use (`images.md`,
`proposal.md`), so a plain request isn't sent rules for pictures or cards; its tool list is filled
from the action registry. Chat text is wrapped in a block named with a random marker that only
the instructions reveal, so the text can't close the block and pose as instructions.
"""

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from chirpo.core.actions import ACTIONS, ActionSpec, Proposal, unsafe_char
from chirpo.core.languages import LANGUAGES
from chirpo.core.permissions import Action
from chirpo.core.tools import ToolSpec, tool_specs
from chirpo.core.workspaces import DEFAULT_PERSONALITY, Personality

MAX_MESSAGE_CHARS = 4000  # a Telegram message is at most 4096; anything longer is cut, not paid for
MAX_SENDER_CHARS = 100


IMAGE_OWN = "Image attached to the chat message:"
IMAGE_REPLIED = "Image attached to the message being replied to:"
IMAGE_RECENT = "Most recent image in the chat, which the chat message may refer to:"
LOOK_TOOL = "look_at_recent_image"


@dataclass(frozen=True, slots=True)
class RequestImage:
    label: str  # says where the image came from; written by us, never by a chat user
    mime_type: str  # what the bytes are, found by looking at them (core/images.py)
    data: bytes


@dataclass(frozen=True, slots=True)
class InterpretRequest:
    # The fixed prompt comes first so providers can cache it; this request's marker ends it.
    instructions: str
    message: str
    tools: tuple[ToolSpec, ...]
    images: tuple[RequestImage, ...] = ()


@dataclass(frozen=True, slots=True)
class Interpretation:
    proposals: tuple[Proposal, ...]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


SMALL_TALK = "small_talk"
# prompts/<name>.md, in the order they are inserted
SECTIONS = ("images", "proposal", "smalltalk", "playful", "language")
_MEDIA_BLOCKS = frozenset({"recent_messages", "replied_message"})
_MEDIA_MARKERS = ("[image", "[file]", "[voice message]")  # written by us, see core/context.py


@cache
def system_prompt(sections: frozenset[str] = frozenset(), language: str | None = None) -> str:
    """`language`: the workspace's issue language, a name from core/languages.py (never typed
    text: it goes into the instructions)."""
    if language is not None:
        if language not in LANGUAGES.values():
            raise ValueError("not a known language")
        sections |= {"language"}
    elif "language" in sections:
        raise ValueError("the language section needs a language")
    unknown = sections - set(SECTIONS)
    if unknown:
        raise ValueError(f"no such prompt section: {sorted(unknown)}")
    prompts = files("chirpo.core").joinpath("prompts")
    template = prompts.joinpath("base.md").read_text(encoding="utf-8")
    extra = "\n\n".join(
        prompts.joinpath(f"{name}.md").read_text(encoding="utf-8").strip()
        for name in SECTIONS
        if name in sections
    )
    tools = "\n".join(
        _describe(spec)
        for spec in ACTIONS.values()
        if spec.name != SMALL_TALK or "smalltalk" in sections  # not offered when personality is off
    )
    text = template.replace("{actions}", tools).replace("{sections}", extra)
    text = text.replace("{language}", language or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()  # no gap where a section was left out


def _sections_for(
    context: Sequence[tuple[str, str]],
    images: Sequence[RequestImage],
    look: bool,
    personality: Personality,
) -> frozenset[str]:
    """The rules this request can use. A marker typed into chat only adds rules, never removes;
    the workspace's personality decides the small talk rules."""
    sections = set()
    if personality != "off":
        sections.add("smalltalk")
    if personality == "playful":
        sections.add("playful")
    if any(kind == "pending_proposal" for kind, _ in context):
        sections.add("proposal")
    media = any(
        marker in body
        for kind, body in context
        if kind in _MEDIA_BLOCKS
        for marker in _MEDIA_MARKERS
    )
    if images or look or media:
        sections.add("images")
    return frozenset(sections)


def interpret_request(
    text: str,
    sender: str,
    *,
    context: Sequence[tuple[str, str]] = (),
    images: Sequence[RequestImage] = (),
    look: bool = False,
    marker: str | None = None,
    personality: Personality = DEFAULT_PERSONALITY,
    issue_language: str | None = None,
) -> InterpretRequest:
    """`context`: (kind, body) blocks from `core.context`, each wrapped like the message.
    `look`: offer the tool that asks to see the most recent image (D52).
    `personality`: the workspace's tone; `off` doesn't offer `small_talk` at all (M6.11).
    `issue_language`: a code from core/languages.py; GitHub text is written in it (M6.4)."""
    marker = marker or secrets.token_hex(8)
    language = LANGUAGES[issue_language] if issue_language else None
    left_out = {
        name
        for name, offered in ((LOOK_TOOL, look), (SMALL_TALK, personality != "off"))
        if not offered
    }
    body = f"From: {_sender(sender)}\n{_clip(text)}"
    blocks = [_untrusted(kind, content, marker) for kind, content in context]
    blocks.append(_untrusted("chat_message", body, marker))
    return InterpretRequest(
        instructions=(
            f"{system_prompt(_sections_for(context, images, look, personality), language)}"
            f"\n\nThis request's marker is `{marker}`."
        ),
        message="\n\n".join(blocks),
        tools=tuple(tool for tool in tool_specs() if tool.name not in left_out),
        images=tuple(images),
    )


def _untrusted(kind: str, body: str, marker: str) -> str:
    while marker in body:  # removing one can join the halves around it into another
        body = body.replace(marker, "")
    return f'<untrusted_{marker} kind="{kind}">\n{body}\n</untrusted_{marker}>'


def _clip(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return f"{text[:MAX_MESSAGE_CHARS]}\n[message cut]"


def _sender(name: str) -> str:
    visible = "".join(char for char in name if not unsafe_char(char))
    return " ".join(visible.split())[:MAX_SENDER_CHARS]


def _describe(spec: ActionSpec) -> str:
    if spec.needs_confirmation:
        note = " chirpo asks for confirmation first."
    elif spec.permission is Action.VIEW:
        note = " Read-only."
    elif spec.permission is not None:
        note = " Runs right away."
    else:
        note = ""
    return f"- `{spec.name}`: {spec.description}{note}"
