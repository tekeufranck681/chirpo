"""What the core sends an LLM and what comes back (D45).

The system prompt lives in `prompts/interpret.md`; its tool list is filled from the action
registry. Chat text is wrapped in a block named with a random marker that only the instructions
reveal, so the text can't close the block and pose as instructions.
"""

import secrets
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from chirpo.core.actions import ACTIONS, ActionSpec, Proposal, unsafe_char
from chirpo.core.permissions import Action
from chirpo.core.tools import ToolSpec, tool_specs

MAX_MESSAGE_CHARS = 4000  # a Telegram message is at most 4096; anything longer is cut, not paid for
MAX_SENDER_CHARS = 100


@dataclass(frozen=True, slots=True)
class InterpretRequest:
    # The fixed prompt comes first so providers can cache it; this request's marker ends it.
    instructions: str
    message: str
    tools: tuple[ToolSpec, ...]


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


def interpret_request(text: str, sender: str, *, marker: str | None = None) -> InterpretRequest:
    marker = marker or secrets.token_hex(8)
    body = f"From: {_sender(sender)}\n{_clip(text)}"
    return InterpretRequest(
        instructions=f"{system_prompt()}\n\nThis request's marker is `{marker}`.",
        message=_untrusted("chat_message", body, marker),
        tools=tool_specs(),
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
