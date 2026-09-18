"""A tiny MCP server, launched by the integration tests over stdio.

Two tools with real schemas, which is all that `expose = "code"` needs to have
something to generate stubs from and something for a `grep` to find.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("ledger")


@server.tool()
def lookup_account(number: str, include_closed: bool = False) -> dict:
    """Find one account by its number.

    Args:
        number: The account number, including the branch prefix.
        include_closed: Also search accounts that have been closed.
    """
    return {"number": number, "closed": include_closed, "balance_cents": 12345}


@server.tool()
def list_branches(region: str = "") -> dict:
    """List branches, optionally in one region.

    Args:
        region: Restrict to a region. Empty means every region.
    """
    return {"region": region, "branches": ["north", "south"]}


if __name__ == "__main__":
    server.run()
