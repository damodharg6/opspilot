"""Output shaping shared by every tool: cap sizes and redact secrets.

LLM context is expensive and tool output is untrusted, so every tool result passes
through here before it leaves the server.
"""

from __future__ import annotations

from typing import Any

from common.redaction import redact

MAX_FIELD_CHARS = 600
MAX_TEXT_CHARS = 12_000
LOG_FIELDS = (
    "timestamp",
    "level",
    "service",
    "event",
    "message",
    "method",
    "path",
    "status",
    "latency_ms",
    "error_type",
    "request_id",
    "stack",
    "rss_mb",
)


def clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = redact(text)
    return (
        text if len(text) <= limit else text[:limit] + f"... [truncated {len(text) - limit} chars]"
    )


def shape_log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep known fields only, clip long values, redact secrets."""
    shaped: dict[str, Any] = {}
    for key in LOG_FIELDS:
        if key in entry:
            value = entry[key]
            shaped[key] = clip(value) if isinstance(value, str) else value
    return shaped


def clip_text(text: str, limit: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    text = redact(text)
    truncated = len(text) > limit
    return {"text": text[:limit], "truncated": truncated, "total_chars": len(text)}
