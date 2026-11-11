"""What the core sends an LLM and what comes back (D45).

The system prompt lives in `prompts/interpret.md`; its tool list is filled from the action
registry. Chat text is wrapped in a block named with a random marker that only the instructions
reveal, so the text can't close the block and pose as instructions.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from chirpo.core.actions import ACTIONS, ActionSpec, Proposal, unsafe_char
from chirpo.core.permissions import Action
from chirpo.core.tools import ToolSpec, tool_specs

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


@cache
def system_prompt() -> str:
    template = files("chirpo.core").joinpath("prompts", "interpret.md").read_text(encoding="utf-8")
    tools = "\n".join(_describe(spec) for spec in ACTIONS.values())
    return template.replace("{actions}", tools).strip()


def interpret_request(
    text: str,
    sender: str,
    *,
    context: Sequence[tuple[str, str]] = (),
    images: Sequence[RequestImage] = (),
    look: bool = False,
    marker: str | None = None,
) -> InterpretRequest:
    """`context`: (kind, body) blocks from `core.context`, each wrapped like the message.
    `look`: offer the tool that asks to see the most recent image (D52)."""
    marker = marker or secrets.token_hex(8)
    body = f"From: {_sender(sender)}\n{_clip(text)}"
    blocks = [_untrusted(kind, content, marker) for kind, content in context]
    blocks.append(_untrusted("chat_message", body, marker))
    return InterpretRequest(
        instructions=f"{system_prompt()}\n\nThis request's marker is `{marker}`.",
        message="\n\n".join(blocks),
        tools=tool_specs() if look else tuple(t for t in tool_specs() if t.name != LOOK_TOOL),
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
