"""Tool definitions for the LLM, generated from the action models, and the way back: a tool call
parsed into a validated proposal (D44).

The schemas are plain JSON Schema with references inlined and nullable unions collapsed, since
several providers handle neither; provider-specific trimming stays in each adapter.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import ValidationError

from chirpo.core.actions import ACTIONS, ActionSpec, Proposal
from chirpo.core.errors import InvalidProposalError

_DROPPED = frozenset({"title", "additionalProperties", "$defs"})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]


@cache
def tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(spec.name, spec.description, _parameters(spec)) for spec in ACTIONS.values()
    )


def parse_tool_call(name: str, arguments: Mapping[str, Any] | None) -> Proposal:
    """`arguments` None means the provider sent none, as some do for tools without fields."""
    spec = ACTIONS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise InvalidProposalError("unknown", ("unknown action",))
    try:
        return spec.model.model_validate(arguments or {})
    except ValidationError as error:
        raise InvalidProposalError(name, _problems(error)) from None


def _problems(error: ValidationError) -> tuple[str, ...]:
    problems = []
    for detail in error.errors(include_input=False, include_url=False):
        parts = [str(part) for part in detail["loc"]]
        if detail["type"] == "extra_forbidden":  # the last part is a key the model made up
            parts[-1:] = ["<unknown field>"]
        path = ".".join(parts)
        problems.append(f"{path}: {detail['type']}" if path else detail["type"])
    return tuple(problems)


def _parameters(spec: ActionSpec) -> dict[str, Any]:
    schema = spec.model.model_json_schema()
    parameters = _clean(schema, schema.get("$defs", {}))
    parameters.pop("description", None)  # the tool's own description says it
    parameters.setdefault("properties", {})
    return parameters


def _clean(node: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return _clean({**defs[node["$ref"].rsplit("/", 1)[-1]], **siblings}, defs)
    variants = node.get("anyOf")
    if variants is not None:
        kept = [variant for variant in variants if variant != {"type": "null"}]
        if len(kept) == 1:
            rest = {key: value for key, value in node.items() if key != "anyOf"}
            if rest.get("default", ...) is None:
                del rest["default"]
            return _clean({**kept[0], **rest}, defs)
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED:
            continue
        if key == "properties":
            cleaned[key] = {name: _clean(field, defs) for name, field in value.items()}
        elif key == "items" and isinstance(value, Mapping):
            cleaned[key] = _clean(value, defs)
        elif key == "anyOf":
            cleaned[key] = [_clean(variant, defs) for variant in value]
        else:
            cleaned[key] = value
    return cleaned
