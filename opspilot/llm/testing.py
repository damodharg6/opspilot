"""Deterministic providers: scripted (unit tests), recording + replay (evals)."""

from __future__ import annotations

from opspilot.llm.types import ChatMessage, LLMError, LLMProvider, LLMResponse, ToolSpec


class ScriptedLLM:
    """Returns pre-written responses in order and keeps every request for assertions."""

    def __init__(self, responses: list[LLMResponse], model: str = "scripted") -> None:
        self.model = model
        self._responses = list(responses)
        self.calls: list[tuple[list[ChatMessage], list[ToolSpec] | None]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        self.calls.append((list(messages), tools))
        if not self._responses:
            raise LLMError("ScriptedLLM ran out of responses")
        return self._responses.pop(0)


class RecordingLLM:
    """Wraps a real provider and keeps its responses so a run can be replayed offline."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.model = inner.model
        self.recorded: list[LLMResponse] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        resp = await self.inner.complete(
            messages, tools, temperature=temperature, max_tokens=max_tokens
        )
        self.recorded.append(resp)
        return resp


class ReplayLLM(ScriptedLLM):
    """Replays recorded responses. Same behaviour as ScriptedLLM, clearer name in evals."""
