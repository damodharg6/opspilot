"""Client for any OpenAI-compatible chat completions API (Groq, Gemini, OpenAI, Ollama...)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx
import openai

from opspilot.llm.types import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSpec, Usage

log = logging.getLogger("opspilot.llm")

RETRYABLE = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


def to_openai_tool_call(tc: ToolCall) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
    }
    if tc.extra_content is not None:  # only providers that sent it get it back
        item["extra_content"] = tc.extra_content
    return item


def to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            item["tool_calls"] = [to_openai_tool_call(tc) for tc in m.tool_calls]
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        out.append(item)
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid_json__": (raw or "")[:500]}
    return value if isinstance(value, dict) else {"__invalid_json__": str(value)[:500]}


def _extra_content(tc: Any) -> dict[str, Any] | None:
    """Non-standard per-call data (Gemini thought signatures); the SDK keeps it in model_extra."""
    value = (getattr(tc, "model_extra", None) or {}).get("extra_content")
    return value if isinstance(value, dict) else None


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
        backoff_base_s: float = 1.0,
    ) -> None:
        self.model = model
        self.backoff_base_s = backoff_base_s
        self.max_retries = max_retries
        # SDK retries off: we own retry policy (backoff + jitter + logging) in one place.
        self._client = openai.AsyncOpenAI(
            base_url=base_url or None,
            api_key=api_key or "not-needed",
            timeout=timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = to_openai_tools(tools)
            kwargs["tool_choice"] = "auto"

        for attempt in range(1, self.max_retries + 2):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                break
            except RETRYABLE as exc:
                if attempt > self.max_retries:
                    raise LLMError(f"LLM unavailable after {attempt} attempts: {exc}") from exc
                delay = min(20.0, self.backoff_base_s * 2 ** (attempt - 1)) * random.uniform(
                    0.8, 1.2
                )
                log.warning(
                    "LLM call failed, retrying",
                    extra={"attempt": attempt, "error_type": type(exc).__name__},
                )
                await asyncio.sleep(delay)
            except openai.APIError as exc:  # auth, bad request, not found: do not retry
                raise LLMError(f"LLM request rejected: {exc}") from exc

        choice = resp.choices[0]
        usage = Usage(
            prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=_parse_arguments(tc.function.arguments),
                extra_content=_extra_content(tc),
            )
            for tc in (choice.message.tool_calls or [])
        ]
        return LLMResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            usage=usage,
            model=resp.model or self.model,
            finish_reason=choice.finish_reason,
        )
