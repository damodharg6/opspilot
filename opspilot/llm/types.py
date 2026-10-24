"""Provider-neutral types for chat + tool calling.

The agent only depends on these. Any model provider (hosted API, local model, a
scripted fake for tests, a replay of recorded responses for evals) plugs in by
implementing LLMProvider.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        return cls(
            content=data.get("content"),
            tool_calls=[ToolCall(**tc) for tc in data.get("tool_calls", [])],
            usage=Usage(**data.get("usage", {})),
            model=data.get("model", ""),
            finish_reason=data.get("finish_reason"),
        )


class LLMError(RuntimeError):
    """The model call failed after retries (auth, quota, outage, bad request)."""


class LLMProvider(Protocol):
    model: str

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> LLMResponse: ...
