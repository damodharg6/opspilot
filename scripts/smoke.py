"""Smoke check for the local stack: health endpoints plus one real MCP tool call.

    python -m scripts.smoke          # or: make smoke

Prints a pass/fail table and exits non-zero if any check fails.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from mcp import Client
from mcp.server import MCPServer
from pydantic_settings import BaseSettings, SettingsConfigDict

TIMEOUT_S = 5.0


class SmokeSettings(BaseSettings):
    """Override any URL with SMOKE_<NAME>, e.g. SMOKE_API_URL=http://localhost:9080."""

    model_config = SettingsConfigDict(env_prefix="SMOKE_", extra="ignore")
    shop_url: str = "http://localhost:8000"
    api_url: str = "http://localhost:8080"
    mcp_logs_url: str = "http://localhost:8101"
    mcp_metrics_url: str = "http://localhost:8102"
    mcp_git_url: str = "http://localhost:8103"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    duration_ms: float


Check = tuple[str, Callable[[], Awaitable[str]]]


async def check_health(url: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(f"{url}/health")
    resp.raise_for_status()
    return f"{resp.status_code} {resp.json().get('status', '')}".strip()


async def check_count_errors(target: str | MCPServer) -> str:
    """Call count_errors over MCP (an HTTP URL, or an in-process server in tests)."""
    async with Client(target) as client:
        result = await asyncio.wait_for(
            client.call_tool("count_errors", {"minutes": 15}), TIMEOUT_S
        )
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise RuntimeError(text[:200] or "tool returned an error")
    return f"total 5xx in 15 min: {json.loads(text)['total']}"


def build_checks(s: SmokeSettings) -> list[Check]:
    return [
        ("shop /health", lambda: check_health(s.shop_url)),
        ("api /health", lambda: check_health(s.api_url)),
        ("mcp-logs /health", lambda: check_health(s.mcp_logs_url)),
        ("mcp-metrics /health", lambda: check_health(s.mcp_metrics_url)),
        ("mcp-git /health", lambda: check_health(s.mcp_git_url)),
        ("mcp-logs count_errors", lambda: check_count_errors(f"{s.mcp_logs_url}/mcp")),
    ]


async def run_check(name: str, check: Callable[[], Awaitable[str]]) -> CheckResult:
    started = time.perf_counter()
    try:
        detail, ok = await check(), True
    except Exception as exc:  # every failure becomes a FAIL row, never a crash
        detail, ok = f"{type(exc).__name__}: {exc}".splitlines()[0][:120], False
    return CheckResult(name, ok, detail, round((time.perf_counter() - started) * 1000, 1))


async def run_checks(checks: list[Check]) -> list[CheckResult]:
    return list(await asyncio.gather(*(run_check(name, fn) for name, fn in checks)))


def render(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results)
    rows = [f"{'CHECK'.ljust(width)}  RESULT  {'MS':>7}  DETAIL"]
    for r in results:
        verdict = "PASS" if r.ok else "FAIL"
        rows.append(f"{r.name.ljust(width)}  {verdict:<6}  {r.duration_ms:>7.1f}  {r.detail}")
    failed = sum(not r.ok for r in results)
    rows.append(f"\n{len(results) - failed}/{len(results)} checks passed")
    return "\n".join(rows)


def main() -> int:
    results = asyncio.run(run_checks(build_checks(SmokeSettings())))
    print(render(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
