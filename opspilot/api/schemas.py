from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IncidentIn(BaseModel):
    service: str = Field(default="shop", min_length=1, max_length=80, pattern=r"^[\w.-]+$")
    alarm_name: str = Field(min_length=1, max_length=200)
    source: str = Field(default="manual", max_length=40)
    description: str = Field(default="", max_length=2000)
    window_minutes: int | None = Field(default=None, ge=1, le=120)
    alarm_time: datetime | None = None


class IncidentAccepted(BaseModel):
    id: uuid.UUID
    status: str
    deduplicated: bool


class ReportOut(BaseModel):
    result_status: str
    category: str
    summary: str
    root_cause: str
    confidence: str
    evidence: list[dict[str, Any]]
    suggested_fix: str
    model: str
    prompt_version: str
    stop_reason: str
    tool_call_count: int
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    evidence_dropped: int


class IncidentOut(BaseModel):
    id: uuid.UUID
    service: str
    alarm_name: str
    source: str
    status: str
    attempts: int
    window_minutes: int
    created_at: datetime
    finished_at: datetime | None
    last_error: str | None
    report: ReportOut | None = None


class ToolCallOut(BaseModel):
    step: int
    tool: str
    arguments: dict[str, Any]
    result_preview: str
    is_error: bool
    duration_ms: float
