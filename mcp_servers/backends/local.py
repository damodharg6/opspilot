"""Local backends: read the shop's JSON log file and a local git repository."""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp_servers.backends.base import (
    BackendError,
    Commit,
    GroupBy,
    MetricName,
    MetricPoint,
    SearchResult,
)
from mcp_servers.query import LogFilter

Clock = Callable[[], datetime]
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class LogFileReader:
    """Reads JSON lines from the log file and its first rotation, inside a time window."""

    def __init__(self, log_file: str | Path, clock: Clock = utcnow) -> None:
        self.path = Path(log_file)
        self.clock = clock

    def entries(self, minutes: int) -> Iterator[tuple[datetime, dict[str, Any]]]:
        since = self.clock() - timedelta(minutes=minutes)
        for path in (self.path.with_name(self.path.name + ".1"), self.path):
            if not path.exists():
                continue
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    ts = _parse_ts(entry.get("timestamp", ""))
                    if ts is not None and ts >= since:
                        yield ts, entry


def _is_request(entry: dict[str, Any]) -> bool:
    return entry.get("event") == "http_request" and isinstance(entry.get("status"), int)


class LocalLogsBackend:
    def __init__(self, log_file: str | Path, clock: Clock = utcnow) -> None:
        self.reader = LogFileReader(log_file, clock)

    def search(self, log_filter: LogFilter, minutes: int, limit: int) -> SearchResult:
        matched = [e for _, e in self.reader.entries(minutes) if log_filter.matches(e)]
        matched.reverse()  # newest first
        return SearchResult(entries=matched[:limit], total_matched=len(matched))

    def count_errors(self, minutes: int, group_by: GroupBy) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for _, entry in self.reader.entries(minutes):
            if _is_request(entry) and entry["status"] >= 500:
                counts[str(entry.get(group_by) or "(none)")] += 1
        return dict(counts.most_common())


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


class LocalMetricsBackend:
    """Derives metrics from the log file, the way CloudWatch metric filters would."""

    def __init__(self, log_file: str | Path, clock: Clock = utcnow) -> None:
        self.reader = LogFileReader(log_file, clock)

    def get_metric(self, name: MetricName, minutes: int, period_s: int) -> list[MetricPoint]:
        buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for ts, entry in self.reader.entries(minutes):
            buckets[int(ts.timestamp()) // period_s * period_s].append(entry)
        points: list[MetricPoint] = []
        for start in sorted(buckets):
            value = self._aggregate(name, buckets[start], period_s)
            if value is not None:
                stamp = datetime.fromtimestamp(start, UTC).isoformat(timespec="seconds")
                points.append(MetricPoint(timestamp=stamp, value=round(value, 2)))
        return points

    @staticmethod
    def _aggregate(name: MetricName, entries: list[dict[str, Any]], period_s: int) -> float | None:
        requests = [e for e in entries if _is_request(e)]
        if name == "memory_mb":
            rss = [float(e["rss_mb"]) for e in entries if e.get("event") == "runtime_stats"]
            return max(rss) if rss else None
        if not requests:
            return None
        if name == "request_rate":
            return len(requests) / period_s
        if name == "error_rate_5xx":
            return 100 * sum(1 for e in requests if e["status"] >= 500) / len(requests)
        latencies = [float(e["latency_ms"]) for e in requests if "latency_ms" in e]
        return _p95(latencies) if latencies else None


class LocalGitBackend:
    def __init__(self, repo_path: str | Path, clock: Clock = utcnow) -> None:
        self.repo = Path(repo_path)
        self.clock = clock

    def _git(self, *args: str) -> str:
        if not (self.repo / ".git").exists():
            raise BackendError(f"no git repository at {self.repo}")
        try:
            result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
                ["git", "-c", "safe.directory=*", "-C", str(self.repo), *args],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as exc:
            raise BackendError(exc.stderr.strip()[:300] or "git command failed") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("git command timed out") from exc
        return result.stdout

    def recent_commits(self, hours: int, limit: int) -> list[Commit]:
        since = (self.clock() - timedelta(hours=hours)).isoformat()
        out = self._git(
            "log",
            f"--since={since}",
            f"--max-count={limit}",
            "--name-only",
            "--date=iso-strict",
            "--pretty=format:\x1e%H\x1f%an\x1f%ad\x1f%s",
        )
        commits: list[Commit] = []
        for block in out.split("\x1e"):
            if not block.strip():
                continue
            header, *files = block.strip("\n").split("\n")
            sha, author, when, subject = header.split("\x1f")
            commits.append(
                Commit(
                    sha=sha,
                    author=author,
                    time=when,
                    message=subject,
                    files=[f for f in files if f],
                )
            )
        return commits

    def commit_diff(self, sha: str) -> str:
        if not SHA_RE.match(sha):
            raise ValueError("sha must be 7-40 lowercase hex characters")
        return self._git("show", "--stat", "--patch", "--date=iso-strict", sha)
