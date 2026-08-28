"""
Tool registry — which tools exist, and which of them a given agent may see.

Two responsibilities, and the second is the interesting one.

Holding tools by name is trivial. *Selecting* them is where the architecture shows up:
a sub-agent is spawned with `tools=["read", "grep"]` and must not be able to reach
`write`, and an agent in plan mode must not be advertised tools the mode forbids —
being told about a capability and then refused it wastes a turn and reads, to the
model, like a bug it should work around.

Selection is therefore by name *and* by permission, and unknown names are an error
rather than a silent omission. A typo in a spawn call that quietly produces a smaller
tool set is the kind of thing that gets diagnosed as "the model is being lazy".
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Sequence

from stcode.core.common.tools import ToolDefinition
from stcode.core.harness.approvals import ApprovalMode, is_forbidden
from stcode.core.harness.tools.base import Tool, ToolError


class ToolRegistry:
    """A named collection of tools."""

    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for entry in tools:
            self.register(entry)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool[Any]]:
        return iter(self._tools.values())

    def __getitem__(self, name: str) -> Tool[Any]:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"No tool named {name!r}. Available: {', '.join(self.names())}.") from None

    def get(self, name: str) -> Tool[Any] | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def register(self, entry: Tool[Any], *, replace: bool = False) -> None:
        """Add a tool. Refuses to shadow an existing name unless asked to.

        Silent replacement is the failure this guards against: an MCP server or a
        `/refine`-authored tool taking over the name `read` would redirect every file
        read in the session with nothing in the log to say so.
        """
        if entry.name in self._tools and not replace:
            raise ToolError(
                f"A tool named {entry.name!r} is already registered. Pass replace=True "
                "if shadowing it is intended."
            )
        self._tools[entry.name] = entry

    def extend(self, tools: Iterable[Tool[Any]], *, replace: bool = False) -> None:
        for entry in tools:
            self.register(entry, replace=replace)

    def select(
        self,
        names: Sequence[str] | None = None,
        *,
        approval_mode: ApprovalMode | None = None,
    ) -> list[Tool[Any]]:
        """The tools an agent may use, by name and by what the mode permits.

        `names=None` means everything registered. Tools the mode forbids outright are
        dropped rather than advertised-and-refused; tools that merely need approval are
        kept, since asking is a normal part of using them.
        """
        if names is None:
            chosen = list(self._tools.values())
        else:
            unknown = [name for name in names if name not in self._tools]
            if unknown:
                raise ToolError(
                    f"Unknown tool(s): {', '.join(unknown)}. Available: {', '.join(self.names())}."
                )
            chosen = [self._tools[name] for name in names]

        if approval_mode is None:
            return chosen
        return [entry for entry in chosen if not is_forbidden(approval_mode, entry.permission)]

    def definitions(
        self,
        names: Sequence[str] | None = None,
        *,
        approval_mode: ApprovalMode | None = None,
    ) -> list[ToolDefinition]:
        """What goes on the wire to a provider.

        Sorted by name so the list is byte-identical between turns with the same tool
        set — §8's prompt-caching lever depends on tool definitions not reordering.
        """
        chosen = self.select(names, approval_mode=approval_mode)
        return [entry.to_tool_definition() for entry in sorted(chosen, key=lambda t: t.name)]


__all__ = ["ToolRegistry"]
