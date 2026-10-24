"""MCP server: read-only service metrics as time series."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_servers.backends.base import BackendError, MetricName, MetricsBackend
from mcp_servers.runtime import make_metrics_backend, serve
from mcp_servers.settings import McpSettings

UNITS = {
    "latency_p95_ms": "milliseconds (95th percentile per period)",
    "error_rate_5xx": "percent of requests returning 5xx",
    "request_rate": "requests per second",
    "memory_mb": "resident memory of the shop process in MB (max per period)",
}


def build_metrics_server(backend: MetricsBackend) -> MCPServer:
    server = MCPServer(
        "metrics",
        instructions="Read-only shop service time series: latency, errors, traffic, memory.",
    )

    @server.tool()
    def get_metric(
        name: Annotated[MetricName, Field(description="Which metric to fetch")],
        minutes: Annotated[int, Field(ge=1, le=180)] = 30,
        period_s: Annotated[int, Field(ge=10, le=3600)] = 60,
    ) -> dict[str, Any]:
        """Fetch a metric as a time series, oldest point first.

        Use it to see WHEN a problem started and whether it is getting worse:
        compare the latest value with the earliest. Metrics: latency_p95_ms,
        error_rate_5xx, request_rate, memory_mb.
        """
        try:
            points = backend.get_metric(name, minutes, period_s)
        except BackendError as exc:
            raise ToolError(str(exc)) from exc
        values = [p.value for p in points]
        return {
            "metric": name,
            "unit": UNITS[name],
            "period_s": period_s,
            "points": [{"timestamp": p.timestamp, "value": p.value} for p in points[-60:]],
            "summary": {
                "count": len(values),
                "min": min(values, default=None),
                "max": max(values, default=None),
                "first": values[0] if values else None,
                "latest": values[-1] if values else None,
            },
        }

    return server


def main() -> None:
    settings = McpSettings()
    serve(build_metrics_server(make_metrics_backend(settings)), settings)


if __name__ == "__main__":
    main()
