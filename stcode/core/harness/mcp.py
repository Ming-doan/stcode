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

## Two ways to expose a server (`[mcp] expose`)

**`"code"` — the default, and the point of step 8.** Nothing is registered. Each tool
is written out as a Python file under `.stcode/mcp_servers/<server>/<tool>.py`, and the
agent finds them with `ls`/`grep`, reads the one it needs, and calls it from `repl`.

Why it matters: tool definitions live in the prompt prefix, so three mid-sized servers
cost 10-30k tokens **per turn, forever**. As code they cost a `grep` and an `import`,
and the result stays in a REPL variable instead of passing through the context.
Anthropic measured this at 150k -> 2k tokens.

**`"tools"` — the old way, kept as an escape hatch.** One small server with two tools
is cheaper advertised directly than discovered over three REPL round-trips. The token
argument is real; it is not universal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import keyword
import logging
import os
import shutil
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

    **One task owns the stack.** The SDK's transports are anyio cancel scopes, and
    anyio refuses to let a scope be exited by a task other than the one that entered
    it. Opening in `Harness.create` and closing in `Harness.aclose` is exactly that —
    two different tasks — so `open()` hands the stack to a task of its own that holds
    it until `aclose()` says stop. Requests may still come from any task; it is only
    entering and leaving that is pinned.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._owner: asyncio.Task[None] | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._stop: asyncio.Event = asyncio.Event()
        self._clients: dict[str, Any] = {}
        self.tools: dict[str, Tool[Any]] = {}
        self.problems: list[str] = []
        """Servers that failed to connect. One broken server must not take down the
        session — the rest of its tools still work, and the agent is told which are
        missing rather than silently getting a shorter tool list."""

    async def open(
        self, servers: dict[str, MCPServerConfig], *, timeout: float = CONNECT_TIMEOUT
    ) -> None:
        """Connect to every server and keep them open until `aclose()`.

        The way to use this class. `connect`/`connect_all` are the pieces it is built
        from, and calling them directly means owning the task problem yourself.
        """
        if self._owner is not None:
            return
        self._owner = asyncio.create_task(self._hold(servers, timeout), name="stcode-mcp")
        await self._ready.wait()

    async def _hold(self, servers: dict[str, MCPServerConfig], timeout: float) -> None:
        """Enter the stack, connect, then sit here until told to stop.

        The whole body runs in one task, which is the point — see the class docstring.
        """
        try:
            async with self._stack:
                await self.connect_all(servers, timeout=timeout)
                self._ready.set()
                await self._stop.wait()
        finally:
            self._ready.set()  # a failure to connect must not leave `open()` waiting
            self._clients.clear()
            self.tools.clear()

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

    def schemas(self) -> dict[str, dict[str, dict[str, Any]]]:
        """`{server: {tool: {description, input_schema}}}` — the generator's input."""
        by_server: dict[str, dict[str, dict[str, Any]]] = {}
        for qualified, entry in self.tools.items():
            _, server, tool = qualified.split("__", 2)
            by_server.setdefault(server, {})[tool] = {
                "description": entry.description,
                "input_schema": entry.input_schema,
            }
        return by_server

    def catalogue(self) -> str:
        """Server and tool names, for the prompt. Names only — the schemas are the
        expensive half, and reading the generated file is how you get them."""
        lines = []
        for server, tools in sorted(self.schemas().items()):
            lines.append(f"- `{server}/` — " + ", ".join(f"`{name}`" for name in sorted(tools)))
        return "\n".join(lines)

    async def aclose(self) -> None:
        """Stop. Unwinds in the task that opened, or here if nothing owns the stack."""
        if self._owner is not None:
            self._stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._owner
            self._owner = None
            return
        await self._stack.aclose()
        self._clients.clear()
        self.tools.clear()

    async def __aenter__(self) -> "MCPManager":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


# ---- MCP as code ----------------------------------------------------------------
#
# Generated stubs run inside the REPL subprocess, which is the same interpreter, so
# they import `call` from here rather than carrying a copy of the client. That keeps
# the generated file small enough to be worth reading, and it keeps credentials out of
# the workspace — the connection is built from the same `.mcp.json` the harness read.

MCP_CODE_DIRNAME = ".stcode/mcp_servers"

_HEADER = '''"""
{title}

Generated by stcode from {source}. Do not edit — regenerated every session.
"""
'''

_REPL_MANAGERS: dict[str, "MCPManager"] = {}
"""One manager per server, in the REPL process. Per server rather than one shared: each
manager owns a task holding its stack (see `MCPManager`), and a second server joining a
manager that is already open would never actually connect."""


async def call(server: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Call one MCP tool. What every generated stub ends up in.

    Connects on first use and holds the connection for the life of the REPL process, so
    a second call to the same server does not pay process startup again.

    Returns structured data when the server provides it, then parsed JSON, then text.
    That order is deliberate: the reason to call a tool from code is to filter its
    result *before* printing, and you cannot filter a paragraph.
    """
    manager = _REPL_MANAGERS.get(server)
    if manager is None:
        servers = load_mcp_config()
        if server not in servers:
            raise ToolError(
                f"No MCP server named {server!r} in the config. Available: "
                f"{', '.join(servers) or '(none)'}."
            )
        manager = MCPManager()
        await manager.open({server: servers[server]})
        if server not in manager._clients:
            problem = "; ".join(manager.problems) or "the server did not start"
            raise ToolError(f"Could not connect to the {server!r} MCP server: {problem}")
        _REPL_MANAGERS[server] = manager

    client = manager._clients[server]
    result = await client.call_tool(tool, {k: v for k, v in arguments.items() if v is not None})
    if getattr(result, "is_error", False):
        raise ToolError(f"{server}.{tool} failed: {_render_content(result)}")
    return _structured(result)


def _structured(result: Any) -> Any:
    """The most useful shape the result comes in."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    text = _render_content(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


_PY_TYPES = {
    "string": "str",
    "number": "float",
    "integer": "int",
    "boolean": "bool",
    "array": "list[Any]",
    "object": "dict[str, Any]",
}


def _annotation(schema: dict[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):  # e.g. ["string", "null"]
        kind = next((k for k in kind if k != "null"), None)
    return _PY_TYPES.get(str(kind), "Any")


def generate_server_code(
    root: Path, servers: dict[str, dict[str, Any]], *, source: str = ".mcp.json"
) -> Path:
    """Write `.stcode/mcp_servers/` from `{server: {tool: schema}}`. Returns the root.

    Rewritten from scratch each session. A stale stub for a tool the server no longer
    has is worse than a missing one — the agent reads it, believes it, and calls
    something that is not there.
    """
    package = root / MCP_CODE_DIRNAME
    if package.exists():
        shutil.rmtree(package, ignore_errors=True)
    package.mkdir(parents=True, exist_ok=True)

    listing = "\n".join(
        f"  {name}/  ->  " + ", ".join(sorted(tools)) for name, tools in sorted(servers.items())
    )
    (package / "__init__.py").write_text(
        _HEADER.format(
            title="MCP servers, as importable Python.\n\n" + (listing or "  (no servers)"),
            source=source,
        )
        + "\n__all__ = [" + ", ".join(repr(name) for name in sorted(servers)) + "]\n",
        encoding="utf-8",
    )

    for server, tools in servers.items():
        directory = package / _identifier(server)
        directory.mkdir(parents=True, exist_ok=True)
        for tool_name, schema in tools.items():
            (directory / f"{_identifier(tool_name)}.py").write_text(
                _render_stub(server, tool_name, schema, source), encoding="utf-8"
            )
        exports = sorted(_identifier(name) for name in tools)
        (directory / "__init__.py").write_text(
            _HEADER.format(title=f"Tools from the {server!r} MCP server.", source=source)
            + "\n"
            + "\n".join(f"from .{name} import {name}" for name in exports)
            + f"\n\n__all__ = [{', '.join(repr(name) for name in exports)}]\n",
            encoding="utf-8",
        )
    return package


def _render_stub(server: str, tool: str, schema: dict[str, Any], source: str) -> str:
    """One tool, as a Python file the agent can read and import.

    The docstring is the whole interface: the server's own description, then one line
    per argument. Reading this file is what replaces having its schema in the prompt.
    """
    properties: dict[str, Any] = schema.get("input_schema", {}).get("properties", {}) or {}
    required = list(schema.get("input_schema", {}).get("required", []) or [])
    function = _identifier(tool)

    parameters, arguments, documented, skipped = [], [], [], []
    for name, spec in properties.items():
        safe = _identifier(name)
        if safe != name:
            # A name Python cannot spell. Passing it positionally is impossible, so say
            # so rather than generating a call the agent cannot make.
            skipped.append(name)
            continue
        annotation = _annotation(spec if isinstance(spec, dict) else {})
        if name in required:
            parameters.append(f"{safe}: {annotation}")
        else:
            parameters.append(f"{safe}: {annotation} | None = None")
        arguments.append(f'"{name}": {safe}')
        description = (spec.get("description") or "").strip().replace("\n", " ") if isinstance(spec, dict) else ""
        documented.append(f"        {safe}: {description or '(no description)'}")

    lines = [_HEADER.format(title=f"{server}.{tool}", source=source)]
    lines.append("from typing import Any\n")
    lines.append("from stcode.core.harness.mcp import call\n")
    lines.append("")
    lines.append(f"async def {function}({', '.join(parameters)}) -> Any:")

    body = [f'    """{(schema.get("description") or f"The {tool} tool.").strip()}']
    if documented:
        body += ["", "    Args:", *documented]
    if skipped:
        body += ["", "    Not callable from here (the server names them in a way Python cannot):",
                 "        " + ", ".join(skipped)]
    body += [
        "",
        "    Returns parsed data when the server sends it, otherwise text.",
        "    Arguments left as None are not sent.",
        '    """',
    ]
    lines += body
    lines.append(f'    return await call("{server}", "{tool}", {{{", ".join(arguments)}}})')
    return "\n".join(lines) + "\n"


def _identifier(name: str) -> str:
    """A name Python can spell, without inventing collisions."""
    cleaned = "".join(char if char.isalnum() or char == "_" else "_" for char in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned + "_" if keyword.iskeyword(cleaned) else cleaned


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


__all__ = [
    "MCPManager",
    "MCPServerConfig",
    "MCP_CODE_DIRNAME",
    "MCP_CONFIG_ENV",
    "call",
    "generate_server_code",
    "load_mcp_config",
]
