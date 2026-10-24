"""Tool gateway: how the agent reaches tools, independent of where they run.

MCPToolGateway connects to MCP servers by URL (production) or to in-process
MCPServer objects (tests, local evals). Recording/replay gateways let an eval run
be repeated offline with exactly the same tool outputs.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from mcp import Client
from mcp.server import MCPServer

from opspilot.llm.types import ToolSpec


@dataclass
class ToolResult:
    text: str
    is_error: bool = False


class ToolGateway(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class MCPToolGateway:
    """Connects to several MCP servers and exposes their tools as one flat list."""

    def __init__(self, servers: dict[str, str | MCPServer], timeout_s: float = 15.0) -> None:
        self._targets = servers
        self._timeout_s = timeout_s
        self._stack = AsyncExitStack()
        self._routes: dict[str, Client] = {}
        self._specs: list[ToolSpec] = []

    async def __aenter__(self) -> MCPToolGateway:
        for server_name, target in self._targets.items():
            client = await self._stack.enter_async_context(Client(target))
            for tool in (await client.list_tools()).tools:
                if tool.name in self._routes:
                    raise ValueError(f"tool name '{tool.name}' exposed by two servers")
                self._routes[tool.name] = client
                self._specs.append(
                    ToolSpec(
                        name=tool.name,
                        description=f"[{server_name}] {tool.description or ''}".strip(),
                        parameters=tool.input_schema,
                    )
                )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        client = self._routes.get(name)
        if client is None:
            return ToolResult(f"unknown tool '{name}'", is_error=True)
        if "__invalid_json__" in arguments:
            return ToolResult("arguments were not valid JSON; send a JSON object", is_error=True)
        try:
            result = await asyncio.wait_for(client.call_tool(name, arguments), self._timeout_s)
        except TimeoutError:
            return ToolResult(f"tool '{name}' timed out after {self._timeout_s}s", is_error=True)
        except Exception as exc:  # transport failures must not crash the investigation
            return ToolResult(f"tool '{name}' failed: {type(exc).__name__}", is_error=True)
        text = "\n".join(getattr(c, "text", "") for c in result.content) or "(empty result)"
        return ToolResult(text=text, is_error=bool(result.is_error))


class RecordingGateway:
    """Wraps a gateway and records every call and result, for offline replay."""

    def __init__(self, inner: ToolGateway) -> None:
        self.inner = inner
        self.specs: list[ToolSpec] = []
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self) -> list[ToolSpec]:
        self.specs = await self.inner.list_tools()
        return self.specs

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self.inner.call(name, arguments)
        self.calls.append({"name": name, "arguments": arguments, "result": asdict(result)})
        return result


class ReplayGateway:
    """Returns recorded results. Fails loudly if the agent asks for something not recorded."""

    def __init__(self, specs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._specs = [ToolSpec(**s) for s in specs]
        self._calls = list(calls)

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._calls:
            return ToolResult("replay: no recorded call left", is_error=True)
        expected = self._calls.pop(0)
        if expected["name"] != name or expected["arguments"] != arguments:
            got = json.dumps({"name": name, "arguments": arguments})
            return ToolResult(f"replay mismatch: got {got}", is_error=True)
        return ToolResult(**expected["result"])
