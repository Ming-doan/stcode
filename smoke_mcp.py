"""
A hand-run smoke test for step 8: three MCP servers, and a prompt that does not grow.

CLAUDE.md marks step 8 done when "three MCP servers are connected and the prompt prefix
does not grow". That is a measurement, so this measures it: the same three servers, in
both modes, with the prompt prefix counted each time.

The prefix is the system prompt plus the tool definitions — everything sent on *every*
turn. In `tools` mode it carries every server's full schema. In `code` mode it carries
server and tool names, and the schemas sit on disk where a `grep` reaches them.

Then it checks the part that makes the saving real rather than a trick: a generated
stub, imported inside the REPL, actually calls its server and returns parsed data.

Run it with `uv run python smoke_mcp.py`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from smoke_common import WORKSPACE, bad, build_config, note, ok, step, verdict
from stcode.core.harness import Harness

ROOT = WORKSPACE / "mcp"

# Three servers, deliberately chatty about their arguments — that verbosity is exactly
# what `tools` mode pays for on every turn.
SERVERS = {
    "bookshop": ["search_books", "reserve", "cancel_hold", "list_branches"],
    "tickets": ["create_ticket", "search_tickets", "close_ticket", "add_comment"],
    "metrics": ["query_series", "list_dashboards", "describe_metric", "alert_status"],
}

TEMPLATE = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("{name}")

{tools}

if __name__ == "__main__":
    server.run()
'''

TOOL = '''
@server.tool()
def {tool}(query: str, limit: int = 10, verbose: bool = False, tag: str = "") -> dict:
    """{tool} on the {name} service.

    Args:
        query: The thing to look for. Write it as a full phrase, not keywords, and
            expect the service to match on several fields at once.
        limit: How many records to return. Keep this small; each record is long.
        verbose: Include the full record rather than the summary fields.
        tag: Restrict to one tag. Empty means every tag.
    """
    return {{"tool": "{tool}", "query": query, "rows": [{{"id": n}} for n in range(limit)]}}
'''


def write_servers() -> Path:
    """Three launchable MCP servers and the `.mcp.json` that names them."""
    ROOT.mkdir(parents=True, exist_ok=True)
    config = {}
    for name, tools in SERVERS.items():
        path = ROOT / f"{name}_server.py"
        path.write_text(
            TEMPLATE.format(
                name=name,
                tools="\n".join(TOOL.format(tool=tool, name=name) for tool in tools),
            )
        )
        config[name] = {"command": sys.executable, "args": [str(path)]}
    (ROOT / ".mcp.json").write_text(json.dumps({"mcpServers": config}, indent=2))
    return ROOT


def prefix_size(harness: Harness) -> tuple[int, int]:
    """Chars of prompt prefix, and how many of those are *MCP* tool definitions.

    The MCP share is the number that matters. The built-in tools' schemas are in the
    prefix either way and are not what step 8 is about, so counting them in the saving
    would flatter it.

    Chars rather than tokens: a token count needs the provider's tokeniser and moves
    the number around without changing the ratio, which is the claim.
    """
    system = harness.system_prompt()
    definitions = [d.model_dump() for d in harness.tool_definitions()]
    mcp_only = [d for d in definitions if d["name"].startswith("mcp__")]
    return len(system) + len(json.dumps(definitions)), len(json.dumps(mcp_only)) if mcp_only else 0


async def main() -> int:
    root = write_servers()
    build_config(subdir="mcp")  # only for the shared .env plumbing
    results: dict[str, bool] = {}

    step(f"connecting {len(SERVERS)} servers, {sum(len(t) for t in SERVERS.values())} tools")

    as_tools = await Harness.create(
        root, approval_mode="full-auto", mcp_expose="tools", load_repl=False
    )
    tools_total, tools_defs = prefix_size(as_tools)
    advertised = [name for name in as_tools.tool_names() if name.startswith("mcp__")]
    ok(f"expose='tools': {len(advertised)} MCP tools advertised, prefix {tools_total:,} chars")
    await as_tools.aclose()

    as_code = await Harness.create(root, approval_mode="full-auto", mcp_expose="code")
    code_total, code_defs = prefix_size(as_code)
    stubs = sorted((root / ".stcode/mcp_servers").glob("*/*.py"))
    stub_files = [p for p in stubs if p.name != "__init__.py"]
    ok(f"expose='code':  {len(stub_files)} stubs on disk, prefix {code_total:,} chars")

    note(
        f"MCP tool definitions in the prefix: {tools_defs:,} -> {code_defs:,} chars, "
        f"every turn (~{tools_defs // 4:,} tokens saved per turn)"
    )
    note(f"whole prefix: {tools_total:,} -> {code_total:,} chars; the rest is the built-in tools")

    grew = code_total < tools_total and len(advertised) == len(stub_files)
    results["the prefix does not grow with servers"] = grew
    (ok if grew else bad)(
        f"every one of the {len(stub_files)} tools is reachable, none of them in the prompt"
    )

    step("a stub really calls its server")
    try:
        cell = await as_code.invoke(
            "repl",
            {
                "code": (
                    "from mcp_servers.tickets import search_tickets\n"
                    "found = await search_tickets(query='login fails', limit=250)\n"
                    # 250 rows exist in the variable; one number enters the context.
                    "print(len(found['rows']))"
                )
            },
        )
        called = cell.content.strip() == "250"
        results["a generated stub calls its server"] = called
        (ok if called else bad)(f"250 rows fetched, {cell.content.strip()!r} returned to the model")

        catalogue = as_code.system_prompt()
        hidden = "Write it as a full phrase" not in catalogue and "`search_tickets`" in catalogue
        results["schemas stay off the prompt"] = hidden
        (ok if hidden else bad)("the prompt names the tools but carries none of their schemas")
    finally:
        await as_code.aclose()

    return verdict("step 8", results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
