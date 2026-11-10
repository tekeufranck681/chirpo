"""Google Gemini through the generateContent REST API (D45).

One stateless call per message: Google keeps no conversation. Tool calls are forced (mode ANY),
every call is validated against the action models, and the API key travels in a header so it
never appears in a URL or a log line. https://ai.google.dev/api/generate-content

Every field of the answer is checked for its type: a proxy page or a format change must end as a
typed `LLMError`, never as a crash in the worker.
"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from chirpo.core.actions import Proposal
from chirpo.core.errors import (
    InvalidProposalError,
    LLMError,
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusedError,
    LLMSetupError,
    LLMUnavailableError,
)
from chirpo.core.llm import Interpretation, InterpretRequest
from chirpo.core.tools import parse_tool_call

API_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"  # 96% on the evals, ~3 s, larger free quota (D48)
DEFAULT_THINKING = "low"  # without it, flash-lite drops the second of two requests (D48)
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Only failures where the request surely never ran are retried: a slow answer is not, since a
# retry would bill the thinking again and keep the person waiting minutes.
RETRY_DELAYS = (1.0, 3.0)
_RETRIED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)
MAX_OUTPUT_TOKENS = 8192  # thinking tokens count too
MAX_PROPOSALS = 10
_MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9.-]*")
_BLOCKED = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)

Sleep = Callable[[float], Awaitable[None]]


class GeminiProvider:
    def __init__(
        self,
        api_key: SecretStr | str,
        model: str = DEFAULT_MODEL,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        thinking: str | None = None,
    ) -> None:
        """`client` is shared and left open (a worker keeps one); without it each call opens one.
        `thinking`: Gemini's thinking level ("minimal" to "high"); None keeps the model's."""
        if not _MODEL_NAME.fullmatch(model):
            raise LLMSetupError(f"CHIRPO_LLM_MODEL={model!r} isn't a Gemini model name")
        self.model = model
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._client = client
        self._sleep = sleep
        self.thinking = thinking

    async def interpret(self, request: InterpretRequest) -> Interpretation:
        return self._interpretation(await self._post(_body(request, self.thinking)))

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                await self._sleep(RETRY_DELAYS[attempt - 1])
            try:
                response = await self._send(body)
            except _RETRIED as error:
                failure = LLMUnavailableError(f"Gemini unreachable: {type(error).__name__}")
                continue
            except httpx.HTTPError as error:
                raise LLMUnavailableError(f"Gemini failed: {type(error).__name__}") from None
            if response.status_code < 500:
                return self._decoded(response)
            failure = LLMUnavailableError(f"Gemini answered {response.status_code}")
        raise failure

    async def _send(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{API_URL}/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self._key.get_secret_value()}
        if self._client is not None:
            return await self._client.post(url, json=body, headers=headers, timeout=TIMEOUT)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await client.post(url, json=body, headers=headers)

    def _decoded(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.is_success:
            if not isinstance(data, dict):
                raise LLMUnavailableError("Gemini sent an answer that isn't a JSON object")
            return data
        raise self._error(response.status_code, _dict(_dict(data).get("error")))

    def _error(self, status: int, error: dict[str, Any]) -> LLMError:
        details = [detail for detail in _list(error.get("details")) if isinstance(detail, dict)]
        if status == 429:
            return LLMRateLimitedError(_retry_delay(details))
        detail = _brief(error.get("message"))
        if status in (401, 403) or any(d.get("reason") == "API_KEY_INVALID" for d in details):
            return LLMSetupError("Gemini rejected the key, check CHIRPO_LLM_API_KEY", detail=detail)
        if status == 404:
            return LLMSetupError(
                f"Gemini has no model {self.model!r}, check CHIRPO_LLM_MODEL", detail=detail
            )
        code = f"{status} {_brief(error.get('status'))}".strip()
        return LLMSetupError(f"Gemini rejected the request ({code})", detail=detail)

    def _interpretation(self, data: dict[str, Any]) -> Interpretation:
        blocked = _dict(data.get("promptFeedback")).get("blockReason")
        if blocked:
            raise LLMRefusedError(_brief(blocked))
        candidates = _list(data.get("candidates"))
        if not candidates:
            raise LLMInvalidOutputError(("no answer",))
        candidate = _dict(candidates[0])
        finish = _brief(candidate.get("finishReason")) or "STOP"
        if finish in _BLOCKED:
            raise LLMRefusedError(finish)
        if finish != "STOP":
            raise LLMInvalidOutputError((finish.lower(),))
        usage = _dict(data.get("usageMetadata"))
        return Interpretation(
            proposals=_proposals(candidate),
            model=_brief(data.get("modelVersion")) or self.model,
            input_tokens=_count(usage.get("promptTokenCount")),
            output_tokens=_count(usage.get("candidatesTokenCount"))
            + _count(usage.get("thoughtsTokenCount")),
        )


def _body(request: InterpretRequest, thinking: str | None) -> dict[str, Any]:
    declarations = [
        {
            "name": tool.name,
            "description": tool.description,
            "parametersJsonSchema": dict(tool.parameters),
        }
        for tool in request.tools
    ]
    config: dict[str, Any] = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
    if thinking is not None:
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    return {
        "systemInstruction": {"parts": [{"text": request.instructions}]},
        "contents": [{"role": "user", "parts": [{"text": request.message}]}],
        "tools": [{"functionDeclarations": declarations}],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        "generationConfig": config,
    }


def _proposals(candidate: dict[str, Any]) -> tuple[Proposal, ...]:
    parts = _list(_dict(candidate.get("content")).get("parts"))
    calls = [_dict(part).get("functionCall") for part in parts]
    calls = [call for call in calls if call is not None]
    if not calls:
        raise LLMInvalidOutputError(("no action proposed",))
    if len(calls) > MAX_PROPOSALS:
        raise LLMInvalidOutputError(("too many actions",))
    proposals: list[Proposal] = []
    problems: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            problems.append("call isn't an object")
            continue
        try:
            proposals.append(parse_tool_call(call.get("name"), _arguments(call.get("args"))))
        except InvalidProposalError as error:
            problems.extend(error.problems)
    if problems:  # one bad call rejects the answer: half a request is worse than asking again
        raise LLMInvalidOutputError(tuple(problems))
    return tuple(proposals)


def _arguments(args: Any) -> dict[str, Any] | None:
    if args is None or isinstance(args, dict):
        return args
    raise InvalidProposalError("unknown", ("arguments aren't an object",))


def _retry_delay(details: list[dict[str, Any]]) -> float | None:
    for detail in details:
        if str(detail.get("@type", "")).endswith("RetryInfo"):
            try:
                seconds = float(str(detail.get("retryDelay", "")).removesuffix("s"))
            except ValueError:
                return None
            return seconds if math.isfinite(seconds) and 0 <= seconds <= 3600 else None
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _brief(value: Any) -> str:
    return " ".join(str(value).split())[:200] if value is not None else ""
