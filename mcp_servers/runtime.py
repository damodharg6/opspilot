"""Backend selection and HTTP serving shared by the three MCP servers."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from common.logging import configure_logging
from mcp_servers.backends.base import GitBackend, LogsBackend, MetricsBackend
from mcp_servers.settings import McpSettings


def make_logs_backend(s: McpSettings) -> LogsBackend:
    if s.backend == "aws":
        from mcp_servers.backends.aws import CloudWatchLogsBackend

        return CloudWatchLogsBackend(s.log_group, s.aws_region)
    from mcp_servers.backends.local import LocalLogsBackend

    return LocalLogsBackend(s.log_file)


def make_metrics_backend(s: McpSettings) -> MetricsBackend:
    if s.backend == "aws":
        from mcp_servers.backends.aws import CloudWatchMetricsBackend

        return CloudWatchMetricsBackend(s.metrics_namespace, s.aws_region)
    from mcp_servers.backends.local import LocalMetricsBackend

    return LocalMetricsBackend(s.log_file)


def make_git_backend(s: McpSettings) -> GitBackend:
    if s.backend == "aws":
        from mcp_servers.backends.aws import GitHubBackend

        return GitHubBackend(s.github_repo, s.github_token.get_secret_value() or None)
    from mcp_servers.backends.local import LocalGitBackend

    return LocalGitBackend(s.git_repo_path)


def serve(server: MCPServer, settings: McpSettings) -> None:
    """Serve over streamable HTTP in stateless mode.

    Stateless + JSON responses means any replica can answer any request, so the
    servers can sit behind a load balancer and scale horizontally.
    """
    configure_logging(f"mcp-{server.name}", settings.log_level)

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": server.name, "backend": settings.backend})

    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=settings.allowed_hosts),
    )
