"""Structured JSON logging shared by every service.

One log record = one JSON object on one line. Field names are stable because the
logs MCP server, CloudWatch metric filters and Logs Insights queries all depend on them.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.context import incident_id_var, request_id_var

# Extra fields a caller may attach with logger.info("...", extra={...}).
STRUCTURED_FIELDS = (
    "event",
    "method",
    "path",
    "status",
    "latency_ms",
    "error_type",
    "client",
    "rss_mb",
    "leak_mb",
    "fault",
    "tool",
    "duration_ms",
    "attempt",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (rid := request_id_var.get()) is not None:
            payload["request_id"] = rid
        if (iid := incident_id_var.get()) is not None:
            payload["incident_id"] = iid
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = payload.get("error_type") or record.exc_info[0].__name__
            payload["stack"] = _short_stack(record)
        return json.dumps(payload, default=str)


def _short_stack(record: logging.LogRecord, max_lines: int = 12) -> str:
    """Last few lines of the traceback: enough to locate the bug, not a wall of text."""
    text = logging.Formatter().formatException(record.exc_info)  # type: ignore[arg-type]
    return "\n".join(text.splitlines()[-max_lines:])


def configure_logging(service: str, level: str = "INFO", log_file: str | None = None) -> None:
    """Send JSON logs to stdout and, optionally, to a rotating file."""
    formatter = JsonFormatter(service)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(log_file, maxBytes=20_000_000, backupCount=3)
        )
    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates our request log line; keep its errors only.
    logging.getLogger("uvicorn.access").disabled = True
