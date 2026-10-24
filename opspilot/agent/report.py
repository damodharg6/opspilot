"""The incident report: the agent's only output, validated before it is stored."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["db_latency", "db_connection", "memory", "bad_deploy", "business", "unknown"]
Confidence = Literal["low", "medium", "high"]
CONFIDENCE_ORDER: list[Confidence] = ["low", "medium", "high"]


class Evidence(BaseModel):
    tool: str = Field(max_length=60)
    snippet: str = Field(
        min_length=1, max_length=500, description="Copied verbatim from tool output"
    )
    timestamp: str | None = Field(default=None, max_length=40)


class IncidentReport(BaseModel):
    summary: str = Field(min_length=1, max_length=600)
    root_cause_category: Category
    root_cause: str = Field(min_length=1, max_length=1200)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    confidence: Confidence
    suggested_fix: str = Field(min_length=1, max_length=800)

    @classmethod
    def unknown(cls, summary: str) -> IncidentReport:
        return cls(
            summary=summary,
            root_cause_category="unknown",
            root_cause="The investigation did not produce a reliable conclusion.",
            evidence=[],
            confidence="low",
            suggested_fix="Investigate manually, starting from the tool trace.",
        )
