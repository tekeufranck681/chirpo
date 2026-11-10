"""Gemini provider against a mocked generateContent (payloads as in Google's API reference)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from chirpo.adapters.llm.gemini import API_URL, DEFAULT_MODEL, GeminiProvider
from chirpo.core.actions import AssignIssue, CreateIssue, IssueRef, SetStatus
from chirpo.core.errors import (
    LLMInvalidOutputError,
    LLMRateLimitedError,
    LLMRefusedError,
    LLMSetupError,
    LLMUnavailableError,
)
from chirpo.core.llm import Interpretation, interpret_request
from chirpo.core.tools import tool_specs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gemini"
KEY = "AIzaSyEXAMPLE-not-a-real-key-000000000"
URL = f"{API_URL}/models/{DEFAULT_MODEL}:generateContent"


def payload(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def answer(*parts: dict[str, Any], finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"parts": list(parts), "role": "model"}, "finishReason": finish}
        ],
        "modelVersion": DEFAULT_MODEL,
    }


def call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if args is not None:
        body["args"] = args
    return {"functionCall": body}


@pytest.fixture
def gemini() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(sleeps: list[float]) -> GeminiProvider:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GeminiProvider(KEY, DEFAULT_MODEL, sleep=sleep)


async def interpret(provider: GeminiProvider, text: str = "hi") -> Interpretation:
    return await provider.interpret(interpret_request(text, "Franck", marker="m7f3a"))


class TestRequest:
    async def test_shape(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        route = gemini.post(URL).respond(json=payload("create_issue"))
        request = interpret_request("checkout crashes", "Franck", marker="m7f3a")
        await provider.interpret(request)

        sent = route.calls.last.request
        assert sent.headers["x-goog-api-key"] == KEY
        assert KEY not in str(sent.url)
        body = json.loads(sent.content)
        assert body["systemInstruction"] == {"parts": [{"text": request.instructions}]}
        assert body["contents"] == [{"role": "user", "parts": [{"text": request.message}]}]
        assert body["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
        declarations = body["tools"][0]["functionDeclarations"]
        assert [d["name"] for d in declarations] == [tool.name for tool in tool_specs()]
        first = declarations[0]
        assert first["description"] == tool_specs()[0].description
        assert first["parametersJsonSchema"]["type"] == "object"
        assert "temperature" not in body.get("generationConfig", {})

    async def test_thinking_level_is_sent_when_chosen(self, gemini: respx.MockRouter) -> None:
        route = gemini.post(URL).respond(json=payload("create_issue"))
        await interpret(GeminiProvider(KEY, thinking="low"))
        config = json.loads(route.calls.last.request.content)["generationConfig"]
        assert config["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_no_thinking_level_leaves_the_models_default(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        route = gemini.post(URL).respond(json=payload("create_issue"))
        await interpret(provider)
        assert (
            "thinkingConfig" not in json.loads(route.calls.last.request.content)["generationConfig"]
        )

    def test_model_names_are_checked(self) -> None:
        with pytest.raises(LLMSetupError):
            GeminiProvider(KEY, "../files")


class TestAnswers:
    async def test_one_call(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(json=payload("create_issue"))
        result = await interpret(provider)
        assert result.proposals == (
            CreateIssue(
                title="Checkout crashes on Android 13",
                body="The app crashes at checkout on Android 13.",
                assignees=("Annie",),
                labels=("bug", "urgent"),
            ),
        )
        assert result.model == "gemini-3.6-flash"
        assert (result.input_tokens, result.output_tokens) == (1834, 61 + 117)

    async def test_several_calls_in_order_and_thoughts_ignored(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        gemini.post(URL).respond(json=payload("two_calls"))
        result = await interpret(provider)
        issue = IssueRef(number=12)
        assert result.proposals == (
            SetStatus(issue=issue, status="in progress"),
            AssignIssue(issue=issue, assignees=("me",)),
        )
        assert result.output_tokens == 48

    async def test_call_without_args(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        gemini.post(URL).respond(json=answer(call("not_a_request")))
        assert len((await interpret(provider)).proposals) == 1

    @pytest.mark.parametrize(
        ("response", "problem"),
        [
            (answer({"text": "Sure, I created it!"}), "no action proposed"),
            ({"candidates": []}, "no answer"),
            (answer(call("delete_repo", {})), "unknown action"),
            (answer(call("create_issue", {"title": ""})), "title: string_too_short"),
            (answer(*[call("not_a_request")] * 11), "too many actions"),
            (
                answer(call("not_a_request"), finish="MALFORMED_FUNCTION_CALL"),
                "malformed_function_call",
            ),
            (answer(finish="MAX_TOKENS"), "max_tokens"),
        ],
    )
    async def test_unusable_answers(
        self,
        provider: GeminiProvider,
        gemini: respx.MockRouter,
        response: dict[str, Any],
        problem: str,
    ) -> None:
        gemini.post(URL).respond(json=response)
        with pytest.raises(LLMInvalidOutputError) as caught:
            await interpret(provider)
        assert problem in caught.value.problems

    async def test_one_bad_call_rejects_the_whole_answer(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        gemini.post(URL).respond(
            json=answer(call("reopen_issue", {"issue": {"number": 3}}), call("create_issue", {}))
        )
        with pytest.raises(LLMInvalidOutputError):
            await interpret(provider)

    async def test_blocked_prompt(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(json=payload("prompt_blocked"))
        with pytest.raises(LLMRefusedError) as caught:
            await interpret(provider)
        assert caught.value.reason == "PROHIBITED_CONTENT"

    async def test_blocked_answer(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(json=answer(finish="SAFETY"))
        with pytest.raises(LLMRefusedError) as caught:
            await interpret(provider)
        assert caught.value.reason == "SAFETY"


class TestErrors:
    async def test_rate_limited_is_not_retried(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        route = gemini.post(URL).respond(429, json=payload("rate_limited"))
        with pytest.raises(LLMRateLimitedError) as caught:
            await interpret(provider)
        assert caught.value.retry_after_seconds == 17
        assert route.call_count == 1

    async def test_rate_limited_without_a_delay(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        gemini.post(URL).respond(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
        with pytest.raises(LLMRateLimitedError) as caught:
            await interpret(provider)
        assert caught.value.retry_after_seconds is None

    async def test_overloaded_is_retried(
        self, provider: GeminiProvider, gemini: respx.MockRouter, sleeps: list[float]
    ) -> None:
        gemini.post(URL).mock(
            side_effect=[
                httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}}),
                httpx.Response(200, json=payload("create_issue")),
            ]
        )
        assert len((await interpret(provider)).proposals) == 1
        assert sleeps == [1.0]

    async def test_gives_up_after_three_tries(
        self, provider: GeminiProvider, gemini: respx.MockRouter, sleeps: list[float]
    ) -> None:
        route = gemini.post(URL).respond(500, json={"error": {"code": 500, "status": "INTERNAL"}})
        with pytest.raises(LLMUnavailableError):
            await interpret(provider)
        assert route.call_count == 3
        assert sleeps == [1.0, 3.0]

    async def test_connection_errors_are_retried(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        route = gemini.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(LLMUnavailableError):
            await interpret(provider)
        assert route.call_count == 3

    async def test_a_slow_answer_is_not_retried(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        route = gemini.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(LLMUnavailableError):
            await interpret(provider)
        assert route.call_count == 1

    @pytest.mark.parametrize("delay", ["inf", "nan", "-5s", "soon", "99999s"])
    async def test_odd_retry_delays_are_ignored(
        self, provider: GeminiProvider, gemini: respx.MockRouter, delay: str
    ) -> None:
        retry = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}
        gemini.post(URL).respond(429, json={"error": {"code": 429, "details": [retry]}})
        with pytest.raises(LLMRateLimitedError) as caught:
            await interpret(provider)
        assert caught.value.retry_after_seconds is None

    async def test_fractional_retry_delay(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        retry = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17.5s"}
        gemini.post(URL).respond(429, json={"error": {"code": 429, "details": [retry]}})
        with pytest.raises(LLMRateLimitedError) as caught:
            await interpret(provider)
        assert caught.value.retry_after_seconds == 17.5

    async def test_bad_key(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(400, json=payload("api_key_invalid"))
        with pytest.raises(LLMSetupError) as caught:
            await interpret(provider)
        assert "CHIRPO_LLM_API_KEY" in str(caught.value)
        assert KEY not in str(caught.value)

    @pytest.mark.parametrize(
        ("status", "hint"), [(403, "CHIRPO_LLM_API_KEY"), (404, "CHIRPO_LLM_MODEL")]
    )
    async def test_setup_problems(
        self, provider: GeminiProvider, gemini: respx.MockRouter, status: int, hint: str
    ) -> None:
        gemini.post(URL).respond(status, json={"error": {"code": status, "message": "nope"}})
        with pytest.raises(LLMSetupError) as caught:
            await interpret(provider)
        assert hint in str(caught.value)

    async def test_googles_explanation_is_kept_apart_from_the_error_text(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        message = 'Invalid JSON payload received. Unknown name "pattern"' + " x" * 300
        gemini.post(URL).respond(
            400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}}
        )
        with pytest.raises(LLMSetupError) as caught:
            await interpret(provider)
        assert str(caught.value) == "Gemini rejected the request (400 INVALID_ARGUMENT)"
        assert 'Unknown name "pattern"' in caught.value.detail
        assert len(caught.value.detail) <= 200

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_proxy_page_instead_of_json_is_still_a_setup_problem(
        self, provider: GeminiProvider, gemini: respx.MockRouter, status: int
    ) -> None:
        gemini.post(URL).respond(status, text="<html>Forbidden</html>")
        with pytest.raises(LLMSetupError):
            await interpret(provider)

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (400, {"error": "boom"}),
            (400, {"error": {"code": 400, "details": None}}),
            (400, {"error": {"code": 400, "details": ["x", 3]}}),
        ],
    )
    async def test_odd_error_bodies(
        self, provider: GeminiProvider, gemini: respx.MockRouter, status: int, body: Any
    ) -> None:
        gemini.post(URL).respond(status, json=body)
        with pytest.raises(LLMSetupError):
            await interpret(provider)

    @pytest.mark.parametrize(
        "body",
        [
            {"candidates": ["x"]},
            {"candidates": "x"},
            {"promptFeedback": "x"},
            {"candidates": [{"content": {"parts": "x"}}]},
            {"candidates": [{"content": {"parts": [{"functionCall": "x"}]}}]},
            [1, 2],
        ],
    )
    async def test_odd_answers_are_invalid_output(
        self, provider: GeminiProvider, gemini: respx.MockRouter, body: Any
    ) -> None:
        gemini.post(URL).respond(json=body)
        with pytest.raises((LLMInvalidOutputError, LLMUnavailableError)):
            await interpret(provider)

    async def test_odd_usage_counts_are_zero(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        body = answer(call("not_a_request"))
        body["usageMetadata"] = {"promptTokenCount": None, "candidatesTokenCount": "many"}
        gemini.post(URL).respond(json=body)
        result = await interpret(provider)
        assert (result.input_tokens, result.output_tokens) == (0, 0)

    async def test_args_that_are_not_an_object(
        self, provider: GeminiProvider, gemini: respx.MockRouter
    ) -> None:
        gemini.post(URL).respond(json=answer({"functionCall": {"name": "my_issues", "args": [1]}}))
        with pytest.raises(LLMInvalidOutputError):
            await interpret(provider)

    async def test_a_shared_client_is_used_and_left_open(self, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(json=payload("create_issue"))
        async with httpx.AsyncClient() as client:
            provider = GeminiProvider(KEY, client=client)
            await interpret(provider)
            await interpret(provider)
            assert not client.is_closed

    async def test_not_json(self, provider: GeminiProvider, gemini: respx.MockRouter) -> None:
        gemini.post(URL).respond(200, text="<html>proxy error</html>")
        with pytest.raises(LLMUnavailableError):
            await interpret(provider)
