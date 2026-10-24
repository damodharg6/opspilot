"""Backend interfaces. Tools depend on these, never on files or AWS directly.

The same MCP tools therefore run against a local log file during development and
against CloudWatch in AWS: switching is one environment variable (MCP_BACKEND).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mcp_servers.query import LogFilter

MetricName = Literal["latency_p95_ms", "error_rate_5xx", "request_rate", "memory_mb"]
GroupBy = Literal["path", "error_type", "status"]


class BackendError(RuntimeError):
    """A backend could not answer (missing repo, API error, timeout). Safe to show the model."""


@dataclass
class SearchResult:
    entries: list[dict[str, Any]]
    total_matched: int


@dataclass
class MetricPoint:
    timestamp: str
    value: float


@dataclass
class Commit:
    sha: str
    author: str
    time: str
    message: str
    files: list[str]


class LogsBackend(Protocol):
    def search(self, log_filter: LogFilter, minutes: int, limit: int) -> SearchResult: ...

    def count_errors(self, minutes: int, group_by: GroupBy) -> dict[str, int]: ...


class MetricsBackend(Protocol):
    def get_metric(self, name: MetricName, minutes: int, period_s: int) -> list[MetricPoint]: ...


class GitBackend(Protocol):
    def recent_commits(self, hours: int, limit: int) -> list[Commit]: ...

    def commit_diff(self, sha: str) -> str: ...
