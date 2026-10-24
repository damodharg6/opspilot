"""MCP server: read-only access to service logs."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_servers.backends.base import BackendError, GroupBy, LogsBackend
from mcp_servers.output import shape_log_entry
from mcp_servers.query import QueryError, parse_query
from mcp_servers.runtime import make_logs_backend, serve
from mcp_servers.settings import McpSettings

Minutes = Annotated[int, Field(ge=1, le=120, description="Look-back window in minutes")]


def build_logs_server(backend: LogsBackend) -> MCPServer:
    server = MCPServer(
        "logs",
        instructions=(
            "Read-only access to the shop service's structured JSON logs. "
            "Start with count_errors to see where errors cluster, then search_logs "
            "to read example lines."
        ),
    )

    @server.tool()
    def search_logs(
        query: Annotated[
            str,
            Field(
                max_length=200,
                description=(
                    "Filter. Field conditions like status>=500, path=/orders, "
                    "error_type=KeyError, level=ERROR, event=http_request, latency_ms>1000; "
                    "other words are matched in the message. Empty string returns all lines."
                ),
            ),
        ] = "",
        minutes: Minutes = 15,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        """Search recent log lines, newest first.

        Use it to read concrete evidence: error types, stack summaries, which paths
        fail, and when failures started. Returns at most `limit` lines plus the total
        number that matched.
        """
        try:
            log_filter = parse_query(query)
        except QueryError as exc:
            raise ToolError(str(exc)) from exc
        try:
            result = backend.search(log_filter, minutes, limit)
        except BackendError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "total_matched": result.total_matched,
            "returned": len(result.entries),
            "truncated": result.total_matched > len(result.entries),
            "entries": [shape_log_entry(e) for e in result.entries],
        }

    @server.tool()
    def count_errors(
        minutes: Minutes = 15,
        group_by: Annotated[GroupBy, Field(description="Field to group 5xx responses by")] = "path",
    ) -> dict[str, Any]:
        """Count HTTP 5xx responses in the window, grouped by path, error_type or status.

        A fast first look at where the service is failing. Zero 5xx does not mean
        everything is fine: also check latency and 4xx patterns.
        """
        try:
            counts = backend.count_errors(minutes, group_by)
        except BackendError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "window_minutes": minutes,
            "group_by": group_by,
            "total": sum(counts.values()),
            "counts": counts,
        }

    return server


def main() -> None:
    settings = McpSettings()
    serve(build_logs_server(make_logs_backend(settings)), settings)


if __name__ == "__main__":
    main()
