"""MCP server: read-only view of recent deploys / commits."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_servers.backends.base import BackendError, GitBackend
from mcp_servers.output import clip, clip_text
from mcp_servers.runtime import make_git_backend, serve
from mcp_servers.settings import McpSettings


def build_git_server(backend: GitBackend) -> MCPServer:
    server = MCPServer(
        "git",
        instructions="Read-only view of recent commits, to correlate incidents with deploys.",
    )

    @server.tool()
    def recent_commits(
        hours: Annotated[int, Field(ge=1, le=168)] = 24,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """List recent commits (newest first) with time, message and files changed.

        Compare commit times with when errors started: a release shortly before an
        error spike is a strong suspect.
        """
        try:
            commits = backend.recent_commits(hours, limit)
        except BackendError as exc:
            raise ToolError(f"git history unavailable: {exc}") from exc
        return {
            "count": len(commits),
            "commits": [
                {
                    "sha": c.sha,
                    "short_sha": c.sha[:8],
                    "time": c.time,
                    "author": clip(c.author, 80),
                    "message": clip(c.message, 200),
                    "files": c.files[:30],
                }
                for c in commits
            ],
        }

    @server.tool()
    def commit_diff(
        sha: Annotated[str, Field(min_length=7, max_length=40, description="Commit sha")],
    ) -> dict[str, Any]:
        """Show one commit's diff (capped in size) to check what a release changed."""
        try:
            diff = backend.commit_diff(sha)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except BackendError as exc:
            raise ToolError(f"could not read commit {sha}: {exc}") from exc
        return {"sha": sha, **clip_text(diff)}

    return server


def main() -> None:
    settings = McpSettings()
    serve(build_git_server(make_git_backend(settings)), settings)


if __name__ == "__main__":
    main()
