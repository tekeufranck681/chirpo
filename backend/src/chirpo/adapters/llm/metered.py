"""Counts every LLM call and its tokens (D57), whatever the provider."""

from typing import Any

from chirpo import metrics
from chirpo.core.errors import LLMError
from chirpo.core.llm import Interpretation, InterpretRequest
from chirpo.core.ports import LLMProvider


class MeteredLLM:
    def __init__(self, inner: LLMProvider, provider: str) -> None:
        self._inner = inner
        self._provider = provider
        self.model = inner.model

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":  # not set yet (copying, a failed __init__): don't recurse
            raise AttributeError(name)
        return getattr(self._inner, name)

    async def interpret(self, request: InterpretRequest) -> Interpretation:
        model = self.model
        try:
            answer = await self._inner.interpret(request)
        except Exception as error:
            # Our own LLM error classes are a short fixed list; anything else is "other".
            kind = type(error).__name__ if isinstance(error, LLMError) else "other"
            metrics.LLM_CALLS.labels(self._provider, model, kind).inc()
            raise
        metrics.LLM_CALLS.labels(self._provider, model, "ok").inc()
        metrics.LLM_TOKENS.labels(self._provider, model, "input").inc(answer.input_tokens)
        metrics.LLM_TOKENS.labels(self._provider, model, "output").inc(answer.output_tokens)
        return answer
