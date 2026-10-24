"""The investigation loop: LLM + tools, inside hard budgets.

    incident -> LLM -> tool calls -> results -> LLM -> ... -> JSON report
                                            -> grounding check -> stored report

Budgets (tool calls, tokens, wall-clock time) are enforced in code, not by asking
the model nicely: an agent that can loop forever is a cost and reliability incident.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from opspilot.agent.grounding import ground_report
from opspilot.agent.prompts import (
    BUDGET_EXHAUSTED,
    SYSTEM_PROMPT,
    IncidentContext,
    incident_message,
    repair_prompt,
    wrap_tool_output,
)
from opspilot.agent.report import IncidentReport
from opspilot.agent.tools import ToolGateway
from opspilot.llm.types import ChatMessage, LLMError, LLMProvider, LLMResponse, ToolSpec, Usage

log = logging.getLogger("opspilot.agent")

Status = Literal["completed", "partial", "failed"]
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Budget:
    max_tool_calls: int = 8
    max_tokens: int = 40_000
    timeout_s: float = 90.0
    temperature: float = 0.0
    max_output_tokens: int = 1500


@dataclass
class ToolStep:
    step: int
    tool: str
    arguments: dict[str, Any]
    result: str
    is_error: bool
    duration_ms: float


@dataclass
class InvestigationResult:
    status: Status
    report: IncidentReport
    steps: list[ToolStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0
    model: str = ""
    stop_reason: str = ""
    evidence_dropped: int = 0
    error: str | None = None


def parse_report(content: str | None) -> IncidentReport:
    """Extract and validate the JSON report from a model reply (tolerates code fences)."""
    if not content:
        raise ValueError("empty reply")
    match = _JSON_BLOCK.search(content)
    if not match:
        raise ValueError("no JSON object found")
    data = json.loads(match.group(0))
    return IncidentReport.model_validate(data)


class Investigator:
    def __init__(self, llm: LLMProvider, tools: ToolGateway, budget: Budget | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.budget = budget or Budget()

    async def run(self, ctx: IncidentContext) -> InvestigationResult:
        started = time.perf_counter()
        state = _RunState()
        try:
            async with asyncio.timeout(self.budget.timeout_s):
                report, stop_reason = await self._investigate(ctx, state)
            status: Status = "completed" if stop_reason == "answered" else "partial"
            error = None
        except TimeoutError:
            report = IncidentReport.unknown(
                f"Investigation timed out after {self.budget.timeout_s:.0f}s "
                f"and {len(state.steps)} tool calls."
            )
            status, stop_reason, error = "partial", "timeout", None
        except (LLMError, ValueError, ValidationError) as exc:
            report = IncidentReport.unknown("Investigation failed before producing a valid report.")
            status, stop_reason, error = "failed", "error", f"{type(exc).__name__}: {exc}"[:500]

        grounded = ground_report(report, [s.result for s in state.steps if not s.is_error])
        result = InvestigationResult(
            status=status,
            report=grounded.report,
            steps=state.steps,
            usage=state.usage,
            latency_s=round(time.perf_counter() - started, 2),
            model=self.llm.model,
            stop_reason=stop_reason,
            evidence_dropped=grounded.dropped,
            error=error,
        )
        log.info(
            "investigation finished",
            extra={"event": "investigation_finished", "duration_ms": result.latency_s * 1000},
        )
        return result

    async def _investigate(
        self, ctx: IncidentContext, state: _RunState
    ) -> tuple[IncidentReport, str]:
        specs = await self.tools.list_tools()
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=incident_message(ctx)),
        ]
        stop_reason = "answered"
        while True:
            if len(state.steps) >= self.budget.max_tool_calls:
                stop_reason = "tool_budget"
                break
            if state.usage.total >= self.budget.max_tokens:
                stop_reason = "token_budget"
                break
            resp = await self._complete(messages, state, tools=specs)
            if not resp.tool_calls:
                return await self._finalize(messages, resp, state), stop_reason
            messages.append(
                ChatMessage(role="assistant", content=resp.content, tool_calls=resp.tool_calls)
            )
            for call in resp.tool_calls:
                if len(state.steps) >= self.budget.max_tool_calls:
                    text = "not executed: tool-call budget reached"
                    messages.append(ChatMessage(role="tool", content=text, tool_call_id=call.id))
                    continue
                t0 = time.perf_counter()
                result = await self.tools.call(call.name, call.arguments)
                state.steps.append(
                    ToolStep(
                        step=len(state.steps) + 1,
                        tool=call.name,
                        arguments=call.arguments,
                        result=result.text,
                        is_error=result.is_error,
                        duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                    )
                )
                content = wrap_tool_output(("ERROR: " if result.is_error else "") + result.text)
                messages.append(ChatMessage(role="tool", content=content, tool_call_id=call.id))

        # Budget exhausted: ask for a final answer without tools.
        messages.append(ChatMessage(role="user", content=BUDGET_EXHAUSTED))
        resp = await self._complete(messages, state, tools=None)
        return await self._finalize(messages, resp, state), stop_reason

    async def _finalize(
        self, messages: list[ChatMessage], resp: LLMResponse, state: _RunState
    ) -> IncidentReport:
        try:
            return parse_report(resp.content)
        except (ValueError, ValidationError) as exc:
            # One repair attempt: show the model its own error.
            messages.append(ChatMessage(role="assistant", content=resp.content))
            messages.append(ChatMessage(role="user", content=repair_prompt(str(exc))))
            retry = await self._complete(messages, state, tools=None)
            return parse_report(retry.content)

    async def _complete(
        self, messages: list[ChatMessage], state: _RunState, tools: list[ToolSpec] | None
    ) -> LLMResponse:
        resp = await self.llm.complete(
            messages,
            tools,
            temperature=self.budget.temperature,
            max_tokens=self.budget.max_output_tokens,
        )
        state.usage.add(resp.usage)
        return resp


@dataclass
class _RunState:
    steps: list[ToolStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
