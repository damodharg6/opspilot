"""The smoke script: MCP tool check, failure reporting and the pass/fail table."""

from pathlib import Path

import pytest

from mcp_servers.backends.local import LocalLogsBackend
from mcp_servers.logs_server import build_logs_server
from scripts.smoke import CheckResult, check_count_errors, render, run_checks
from tests.test_mcp_servers import clock, log_file  # noqa: F401 - fixture reuse


async def test_count_errors_check_reads_real_tool_output(log_file: Path) -> None:  # noqa: F811
    server = build_logs_server(LocalLogsBackend(log_file, clock))
    assert await check_count_errors(server) == "total 5xx in 15 min: 2"


async def test_failing_check_becomes_fail_row_not_crash() -> None:
    async def ok() -> str:
        return "200 ok"

    async def broken() -> str:
        raise ConnectionError("connection refused\nmore detail")

    results = await run_checks([("good", ok), ("bad", broken)])
    assert [(r.name, r.ok) for r in results] == [("good", True), ("bad", False)]
    assert results[1].detail == "ConnectionError: connection refused"


@pytest.mark.parametrize(("oks", "summary"), [((True, True), "2/2"), ((True, False), "1/2")])
def test_render_shows_verdicts_and_summary(oks: tuple[bool, bool], summary: str) -> None:
    results = [CheckResult(f"c{i}", ok, "x", 1.0) for i, ok in enumerate(oks)]
    out = render(results)
    assert ("FAIL" in out) == (not all(oks))
    assert out.endswith(f"{summary} checks passed")
