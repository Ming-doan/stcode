"""
MCP client — external tool servers, loaded from a JSON config.

CLAUDE.md's non-goals say we are not reimplementing MCP, and this module takes that
literally: the official `mcp` SDK owns the protocol, and everything here is the
adapter between it and `harness/tools/base.py`. Two things happen at this boundary.

**Schemas pass through untouched.** A server describes its own tools, and that
description is authoritative. `Tool` accepts it via `input_schema` and skips the
pydantic model it would otherwise derive — validating an MCP call against a model we
invented would reject calls the server would have accepted.

**Names are namespaced.** `mcp__<server>__<tool>`, the same convention Claude Code
uses. Two servers offering `search` is the normal case, not the exotic one, and a
collision that silently shadows one of them is very hard to notice from the outside.

Config is the familiar `mcpServers` object, so an existing `.mcp.json` works unchanged:

    {"mcpServers": {
      "fs":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
      "docs":   {"type": "http", "url": "https://example.com/mcp"}
    }}

Every MCP tool is declared `EXECUTE`. We cannot see what a server does — the name says
"search" and the implementation may write files — so the permission reflects what is
actually known, which is nothing.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.tools.base import Runtime, Tool, ToolError

logger = logging.getLogger("stcode.harness.mcp")

MCP_CONFIG_ENV = "STCODE_MCP_CONFIG"
MCP_CONFIG_NAMES = (".mcp.json", "mcp.json")
CONNECT_TIMEOUT = 30.0
MCP_MAX_OUTPUT = 16384


class MCPServerConfig(BaseModel):
    """One server entry. Either a command to launch, or a URL to connect to."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    type: str | None = None
    enabled: bool = True

    def describe(self) -> str:
        return self.url or " ".join([self.command or "?", *self.args])


def load_mcp_config(path: str | Path | None = None, cwd: Path | None = None) -> dict[str, MCPServerConfig]:
    """Read `mcpServers` from the first config file found.

    Search order is explicit path, `$STCODE_MCP_CONFIG`, then `.mcp.json` in the
    project. A missing config is not an error — most sessions have no MCP servers, and
    that is a normal state rather than a misconfiguration.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    if os.environ.get(MCP_CONFIG_ENV):
        candidates.append(Path(os.environ[MCP_CONFIG_ENV]).expanduser())
    root = cwd or Path.cwd()
    candidates += [root / name for name in MCP_CONFIG_NAMES]
    candidates.append(Path.home() / ".stcode" / "mcp.json")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(f"Could not read MCP config {candidate}: {exc}") from exc
        servers = data.get("mcpServers", data)
        return {
            name: MCPServerConfig.model_validate(entry)
            for name, entry in servers.items()
            if isinstance(entry, dict)
        }
    return {}


class MCPManager:
    """Owns the live connections and the tools they expose.

    Connections are opened once for the session and held on an `AsyncExitStack`, not
    reopened per call: a stdio server is a subprocess, and paying process startup on
    every tool call turns a 20 ms call into a 2 s one. `aclose()` unwinds them in
    reverse, which is what stops a Ctrl-C from leaving orphaned server processes (§8).
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._clients: dict[str, Any] = {}
        self.tools: dict[str, Tool[Any]] = {}
        self.problems: list[str] = []
        """Servers that failed to connect. One broken server must not take down the
        session — the rest of its tools still work, and the agent is told which are
        missing rather than silently getting a shorter tool list."""

    async def connect_all(
        self, servers: dict[str, MCPServerConfig], *, timeout: float = CONNECT_TIMEOUT
    ) -> None:
        for name, config in servers.items():
            if not config.enabled:
                continue
            try:
                await self.connect(name, config, timeout=timeout)
            except Exception as exc:
                self.problems.append(f"{name} ({config.describe()}): {exc}")
                logger.warning("MCP server %s failed to connect: %s", name, exc)

    async def connect(self, name: str, config: MCPServerConfig, *, timeout: float = CONNECT_TIMEOUT) -> None:
        import asyncio

        from mcp import StdioServerParameters
        from mcp.client import Client

        if config.url:
            target: Any = config.url
        elif config.command:
            target = StdioServerParameters(
                command=config.command,
                args=config.args,
                # Layered over the real environment, not replacing it: a server launched
                # with only its own `env` loses PATH and cannot find its interpreter.
                env={**os.environ, **config.env},
                cwd=config.cwd,
            )
        else:
            raise ToolError(f"MCP server {name!r} has neither `command` nor `url`.")

        client = await asyncio.wait_for(self._stack.enter_async_context(Client(target)), timeout)
        self._clients[name] = client

        listing = await asyncio.wait_for(client.list_tools(), timeout)
        for remote in listing.tools:
            wrapped = self._wrap(name, client, remote)
            self.tools[wrapped.name] = wrapped

    def _wrap(self, server: str, client: Any, remote: Any) -> Tool[Any]:
        qualified = f"mcp__{server}__{remote.name}"

        async def call(runtime: Runtime[Any], **arguments: Any) -> str:
            result = await client.call_tool(remote.name, arguments)
            text = _render_content(result)
            if getattr(result, "is_error", False):
                raise ToolError(f"{qualified} failed: {text}")
            return text

        return Tool(
            call,
            name=qualified,
            description=(remote.description or f"Tool {remote.name} from the {server!r} MCP server.").strip(),
            permission=ToolPermission.EXECUTE,
            max_output=MCP_MAX_OUTPUT,
            input_schema=remote.input_schema or {"type": "object", "properties": {}},
        )

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._clients.clear()
        self.tools.clear()

    async def __aenter__(self) -> "MCPManager":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


def _render_content(result: Any) -> str:
    """Flatten an MCP `CallToolResult` into text.

    Servers return a list of typed content blocks. Text blocks are joined; anything
    else is named rather than dropped, because "the server returned an image" is
    information the agent can act on and an empty string is not.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'content')} omitted — not text]")
    if not parts and getattr(result, "structured_content", None):
        return json.dumps(result.structured_content, indent=2, default=str)
    return "\n".join(parts) or "(no content)"


__all__ = ["MCPManager", "MCPServerConfig", "MCP_CONFIG_ENV", "load_mcp_config"]
