"""MCP servers are tested through a real MCP client connected in memory."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client

from mcp_servers.backends.local import LocalGitBackend, LocalLogsBackend, LocalMetricsBackend
from mcp_servers.git_server import build_git_server
from mcp_servers.logs_server import build_logs_server
from mcp_servers.metrics_server import build_metrics_server
from mcp_servers.query import QueryError, parse_query

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def line(seconds_ago: float, **fields: object) -> str:
    ts = (NOW - timedelta(seconds=seconds_ago)).isoformat(timespec="milliseconds")
    return json.dumps({"timestamp": ts, "level": "INFO", "service": "shop", **fields})


@pytest.fixture()
def log_file(tmp_path: Path) -> Path:
    lines = [
        line(3000, event="http_request", method="GET", path="/products", status=200, latency_ms=12),
        line(400, event="http_request", method="GET", path="/products", status=200, latency_ms=15),
        line(300, event="runtime_stats", rss_mb=110.0, message="runtime stats"),
        line(
            120,
            level="ERROR",
            event="http_request",
            method="POST",
            path="/orders",
            status=500,
            latency_ms=20,
            error_type="KeyError",
            message="POST /orders 500 password=hunter2",
            request_id="r1",
        ),
        line(
            60,
            level="ERROR",
            event="http_request",
            method="POST",
            path="/orders",
            status=500,
            latency_ms=900,
            error_type="KeyError",
            message="POST /orders 500",
        ),
        line(30, event="runtime_stats", rss_mb=180.0, message="runtime stats"),
        "not json at all",
    ]
    path = tmp_path / "shop.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


# --- query language ----------------------------------------------------------------------
def test_parse_query_conditions_and_terms() -> None:
    f = parse_query('status>=500 path=/orders "key error"')
    assert len(f.conditions) == 2 and f.terms == ("key error",)


@pytest.mark.parametrize(
    "bad", ["hostname=x", "status>=abc", "path>/orders", "path=/a;drop", "x" * 201]
)
def test_parse_query_rejects_unsafe_input(bad: str) -> None:
    with pytest.raises(QueryError):
        parse_query(bad)


# --- logs server ---------------------------------------------------------------------------
async def test_search_logs_filters_window_and_redacts(log_file: Path) -> None:
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    async with Client(server) as c:
        data = payload(await c.call_tool("search_logs", {"query": "status>=500", "minutes": 15}))
    assert data["total_matched"] == 2
    newest, _oldest = data["entries"]
    assert newest["latency_ms"] == 900  # newest first
    assert "hunter2" not in json.dumps(data)


async def test_search_logs_window_excludes_old_lines(log_file: Path) -> None:
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    async with Client(server) as c:
        data = payload(await c.call_tool("search_logs", {"query": "path=/products", "minutes": 15}))
    assert data["total_matched"] == 1


async def test_search_logs_bad_query_is_tool_error(log_file: Path) -> None:
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    async with Client(server) as c:
        result = await c.call_tool("search_logs", {"query": "hostname=evil"})
    assert result.is_error and "unknown field" in result.content[0].text


async def test_search_logs_rejects_out_of_range_args(log_file: Path) -> None:
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    async with Client(server) as c:
        result = await c.call_tool("search_logs", {"query": "", "minutes": 100000})
    assert result.is_error


async def test_count_errors_groups(log_file: Path) -> None:
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    async with Client(server) as c:
        data = payload(await c.call_tool("count_errors", {"group_by": "error_type"}))
    assert data == {
        "window_minutes": 15,
        "group_by": "error_type",
        "total": 2,
        "counts": {"KeyError": 2},
    }


# --- metrics server ------------------------------------------------------------------------
async def test_metrics_from_logs(log_file: Path) -> None:
    server = build_metrics_server(LocalMetricsBackend(log_file, clock))
    async with Client(server) as c:
        mem = payload(await c.call_tool("get_metric", {"name": "memory_mb", "minutes": 15}))
        err = payload(await c.call_tool("get_metric", {"name": "error_rate_5xx", "minutes": 15}))
        names = [t.name for t in (await c.list_tools()).tools]
    assert names == ["get_metric"]
    assert mem["summary"]["first"] == 110.0 and mem["summary"]["latest"] == 180.0
    assert err["summary"]["max"] == 100.0


async def test_metric_name_is_validated(log_file: Path) -> None:
    server = build_metrics_server(LocalMetricsBackend(log_file, clock))
    async with Client(server) as c:
        assert (await c.call_tool("get_metric", {"name": "cpu"})).is_error


# --- git server ----------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pricing.py").write_text("RATE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release v1.5.0: promotion pricing")
    return repo


async def test_recent_commits_and_diff(repo: Path) -> None:
    server = build_git_server(LocalGitBackend(repo))
    async with Client(server) as c:
        commits = payload(await c.call_tool("recent_commits", {"hours": 24}))
        sha = commits["commits"][0]["sha"]
        diff = payload(await c.call_tool("commit_diff", {"sha": sha}))
    assert commits["count"] == 1
    assert commits["commits"][0]["files"] == ["pricing.py"]
    assert "+RATE = 1" in diff["text"]
    assert "dev@example.com" not in diff["text"]  # redacted


async def test_commit_diff_rejects_non_sha(repo: Path) -> None:
    server = build_git_server(LocalGitBackend(repo))
    async with Client(server) as c:
        result = await c.call_tool("commit_diff", {"sha": "HEAD; rm -rf /"})
    assert result.is_error


async def test_missing_repo_is_tool_error(tmp_path: Path) -> None:
    server = build_git_server(LocalGitBackend(tmp_path / "nope"))
    async with Client(server) as c:
        result = await c.call_tool("recent_commits", {})
    assert result.is_error and "unavailable" in result.content[0].text
