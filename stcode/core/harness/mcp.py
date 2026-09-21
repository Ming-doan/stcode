"""
MCP client — external tool servers, loaded from a JSON config.

We do not reimplement MCP: the official `mcp` SDK owns the protocol, and this is the
adapter to `harness/tools/base.py`. Config is the familiar `mcpServers` object, so an
existing `.mcp.json` works unchanged:

    {"mcpServers": {
      "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
      "docs": {"type": "http", "url": "https://example.com/mcp"}
    }}

Three rules at the boundary:

* **Schemas pass through untouched.** The server's own description is authoritative;
  validating against a model we invented would reject calls it would have accepted.
* **Names are namespaced** `mcp__<server>__<tool>`. Two servers offering `search` is
  normal, and a silent shadow is very hard to notice from outside.
* **Every MCP tool is `EXECUTE`.** We cannot see what a server does — the name says
  "search" and the code may write files — so the permission reflects what is known.

## Two ways to expose a server (`[mcp] expose`)

**`"code"`, the default.** Nothing is registered. Each tool becomes a Python file under
`.stcode/mcp_servers/<server>/<tool>.py`; the agent greps, reads the one it needs, and
calls it from `repl`. Tool definitions live in the prompt prefix, so three mid-sized
servers cost 10-30k tokens *per turn, forever*; as code they cost a grep and an import,
and the result stays in a REPL variable.

**`"tools"`, the escape hatch.** One small server with two tools is cheaper advertised
directly than discovered over three REPL round-trips. The token argument is real but
not universal.
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
USER_MCP_DIR = Path.home() / ".stcode"
"""Where the global config lives. A module constant, like `USER_SKILLS_DIR`, so a test
can move it — `$HOME` is read at import time and setting it later is too late."""

CONNECT_TIMEOUT = 30.0
MCP_MAX_OUTPUT = 16384


class MCPServerConfig(BaseModel):
    """One server entry. Either a command to launch, or a URL to connect to."""

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    """Sent with every request to a `url` server. This is how a hosted server is
    authenticated — the key is a header, not an argument — so dropping the field would
    leave a configured credential silently unsent and the server answering as anonymous."""

    type: str | None = None
    enabled: bool = True

    def describe(self) -> str:
        return self.url or " ".join([self.command or "?", *self.args])


def mcp_config_paths(path: str | Path | None = None, cwd: Path | None = None) -> list[Path]:
    """Every config that applies, **lowest precedence first**.

    Global (`~/.stcode/mcp.json`), then the project's, then whatever was named
    explicitly. The order is the merge order, so the last file to mention a server name
    is the one that owns it.
    """
    candidates: list[Path] = [USER_MCP_DIR / name for name in MCP_CONFIG_NAMES]
    root = cwd or Path.cwd()
    candidates += [root / name for name in MCP_CONFIG_NAMES]
    if os.environ.get(MCP_CONFIG_ENV):
        candidates.append(Path(os.environ[MCP_CONFIG_ENV]).expanduser())
    if path:
        candidates.append(Path(path).expanduser())
    return [candidate for candidate in candidates if candidate.is_file()]


def load_mcp_config(path: str | Path | None = None, cwd: Path | None = None) -> dict[str, MCPServerConfig]:
    """Read `mcpServers` from every config that applies, merged by server name.

    **Merged, not first-wins.** A global `~/.stcode/mcp.json` holds the servers you want
    everywhere (context7, a docs server); a project's `.mcp.json` holds the ones this
    repository needs and is checked in. Making one of them shadow the other entirely
    means adding a single project server silently unplugs every global one.

    The unit of precedence is the **whole entry**, not its fields: a name defined in two
    places takes the later file's definition outright. Half a command from one file and
    half from another is a server nobody configured.
    """
    servers: dict[str, MCPServerConfig] = {}
    for candidate in mcp_config_paths(path, cwd):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(f"Could not read MCP config {candidate}: {exc}") from exc
        entries = data.get("mcpServers", data)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if isinstance(entry, dict):
                servers[name] = MCPServerConfig.model_validate(entry)
    return servers


class MCPManager:
    """Owns the live connections and the tools they expose.

    Opened once per session and held on an `AsyncExitStack`, not reopened per call: a
    stdio server is a subprocess, and process startup turns a 20 ms call into a 2 s one.

    **One task owns the stack.** The SDK's transports are anyio cancel scopes, and
    anyio refuses to let one be exited by a task other than the one that entered it.
    Opening in `Harness.create` and closing in `Harness.aclose` are different tasks, so
    `open()` hands the stack to a task of its own. Requests may come from any task; only
    entering and leaving is pinned.
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
        session; the agent is told which are missing rather than silently getting a
        shorter tool list."""

    async def open(
        self, servers: dict[str, MCPServerConfig], *, timeout: float = CONNECT_TIMEOUT
    ) -> None:
        """Connect to every server and keep them open until `aclose()`.

        The way to use this class. `connect`/`connect_all` are its pieces; calling them
        directly means owning the task problem yourself.
        """
        if self._owner is not None:
            return
        self._owner = asyncio.create_task(self._hold(servers, timeout), name="stcode-mcp")
        await self._ready.wait()

    async def _hold(self, servers: dict[str, MCPServerConfig], timeout: float) -> None:
        """Enter the stack, connect, then sit here until told to stop. One task, which
        is the point — see the class docstring."""
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

        if config.url and config.headers:
            # A URL alone is enough for the SDK, but a URL *with headers* is not: the
            # string form builds its own HTTP client and there is nowhere to put them.
            # So the transport is built here instead — the alternative is accepting a
            # configured API key and never sending it.
            # `create_mcp_http_client` is public and documented but absent from the
            # module's `__all__`, which is a packaging detail of the SDK rather than a
            # statement about the function.
            from mcp.client.streamable_http import (  # type: ignore[attr-defined]
                create_mcp_http_client,
                streamable_http_client,
            )

            target: Any = streamable_http_client(
                config.url, http_client=create_mcp_http_client(dict(config.headers))
            )
        elif config.url:
            target = config.url
        elif config.command:
            target = StdioServerParameters(
                command=config.command,
                args=config.args,
                # Layered over the real environment: a server given only its own `env`
                # loses PATH and cannot find its interpreter.
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
        """Server and tool names for the prompt. Names only — the schemas are the
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
# Stubs run in the REPL subprocess — the same interpreter — so they import `call` from
# here instead of carrying a copy of the client. That keeps each generated file small
# enough to be worth reading, and credentials out of the workspace.

MCP_CODE_DIRNAME = ".stcode/mcp_servers"

_HEADER = '''"""
{title}

Generated by stcode from {source}. Do not edit — regenerated every session.
"""
'''

_REPL_MANAGERS: dict[str, tuple[Any, "MCPManager"]] = {}
"""One manager per server, in the REPL process, **with the loop it was opened on**.

Each manager owns a task holding its stack, so a second server joining an already-open
manager would never actually connect. The loop is remembered because a connection does
not outlive it: a cell that runs `asyncio.run(...)` gets a fresh loop, and closing it
cancels the task holding the stack, whose `finally` empties `_clients`. Cached without
the loop, the next call finds a manager that is still in the dict and no longer
connected — which surfaced as a bare `KeyError: '<server>'` from inside the stub, three
layers below anything that could explain it.
"""


def _live_manager(server: str) -> "MCPManager | None":
    """The cached manager for `server`, if it is still usable from this task.

    Same loop, still holding a client. Anything else is discarded rather than returned:
    a stale entry is why reconnecting was impossible without restarting the REPL.
    """
    cached = _REPL_MANAGERS.get(server)
    if cached is None:
        return None
    loop, manager = cached
    if loop is asyncio.get_running_loop() and server in manager._clients:
        return manager
    _REPL_MANAGERS.pop(server, None)
    return None


async def call(server: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Call one MCP tool. What every generated stub ends up in.

    Connects on first use and holds the connection for as long as the event loop that
    opened it lives — which, in the REPL, is the whole session unless a cell closes it.

    Returns structured data when the server provides it, then parsed JSON, then text —
    the reason to call a tool from code is to filter before printing, and you cannot
    filter a paragraph.
    """
    manager = _live_manager(server)
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
        _REPL_MANAGERS[server] = (asyncio.get_running_loop(), manager)

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

    Rewritten from scratch each session: a stale stub is worse than a missing one, as
    the agent reads it, believes it, and calls something that is not there.
    """
    package = root / MCP_CODE_DIRNAME
    if package.exists():
        shutil.rmtree(package, ignore_errors=True)
    package.mkdir(parents=True, exist_ok=True)
    # Generated from the config on every session, so committing it would be committing
    # a build artefact that is wrong the moment a server changes. The directory ignores
    # itself rather than asking every project to edit its own `.gitignore` — and because
    # the tree is deleted and rewritten each session, this file is written each time too.
    (package / ".gitignore").write_text("*\n", encoding="utf-8")

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

    The docstring is the whole interface — the server's description, then one line per
    argument. Reading this file replaces having its schema in the prompt.
    """
    properties: dict[str, Any] = schema.get("input_schema", {}).get("properties", {}) or {}
    required = list(schema.get("input_schema", {}).get("required", []) or [])
    function = _identifier(tool)

    parameters, arguments, documented, skipped = [], [], [], []
    for name, spec in properties.items():
        safe = _identifier(name)
        if safe != name:
            # A name Python cannot spell. Say so rather than generating a call the
            # agent cannot make.
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

    Text blocks are joined; other block types are named rather than dropped — "the
    server returned an image" is actionable, an empty string is not.
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
    "MCP_CONFIG_NAMES",
    "USER_MCP_DIR",
    "call",
    "generate_server_code",
    "load_mcp_config",
    "mcp_config_paths",
]
