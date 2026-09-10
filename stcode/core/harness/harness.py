"""
Harness — the one object the agent loop talks to.

Everything under `core/harness/` is assembled here: the tools an agent may use, the
skills it can pull in, the MCP servers it is connected to, the prompt it wakes up with,
and the `Runtime` that carries all of it into a tool call. The agent loop should need
exactly two verbs from this module — "what tools do I advertise" and "run this call" —
and know nothing about how either is put together.

    harness = await Harness.create(cwd=project_root, approval_mode="auto-edit")
    system  = harness.system_prompt()
    tools   = harness.tool_definitions()
    result  = await harness.invoke("read", {"path": "src/main.py"}, tool_call_id=call.id)

One decision worth naming: a `Harness` is **per agent**, not per process. Its context
carries a working directory, a write scope, and an approval mode, and CLAUDE.md §2.2
rule 4 depends on two sub-agents having *different* write scopes at the same time.
`for_subagent()` is the supported way to make the narrowed copy — sharing one harness
and mutating its scope between spawns is exactly the bug that rule exists to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Iterator, MutableMapping, Sequence

from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.harness.context import HarnessContext, git_context
from stcode.core.harness.mcp import MCPManager, load_mcp_config
from stcode.core.harness.prompts import PromptMode, build_system_prompt, mode_for
from stcode.core.harness.registry import ToolRegistry
from stcode.core.harness.skills import SkillRegistry
from stcode.core.harness.tools import BUILTIN_TOOLS, MAIN_TOOLS, WORKER_TOOLS
from stcode.core.harness.tools.base import (
    ApprovalFn,
    AskFn,
    ProgressFn,
    Runtime,
    Tool,
    current_runtime,
)
from stcode.core.repl import PyREPL

PROJECT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".stcode/instructions.md")
"""Read in order; the first that exists wins. `AGENTS.md` is included because it is the
cross-agent convention and a repository that has one has already written down how it
wants to be worked in."""


class Harness:
    """One agent's capabilities: its tools, its skills, its prompt, its permissions."""

    def __init__(
        self,
        context: HarnessContext | None = None,
        *,
        tools: Sequence[Tool[Any]] | None = None,
        allowed: Sequence[str] | None = None,
        approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE,
        session_id: str = "",
        agent_name: str = "main",
        depth: int = 0,
        project_instructions: str = "",
        extra_prompt: str = "",
        outputs: MutableMapping[str, Any] | None = None,
        on_progress: ProgressFn | None = None,
        on_approval: ApprovalFn | None = None,
        on_ask: AskFn | None = None,
        mcp: MCPManager | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        self.context = context if context is not None else HarnessContext()
        self.registry = ToolRegistry(tools if tools is not None else BUILTIN_TOOLS)
        # An explicit `tools=` list is already the caller's selection, so allowing all
        # of it is right. The built-in set is not: `MAIN_TOOLS` is the narrower default
        # (see `tools/__init__.py`).
        if allowed is not None:
            self.allowed: list[str] | None = list(allowed)
        elif tools is not None:
            self.allowed = None
        else:
            self.allowed = list(MAIN_TOOLS)
        self.approval_mode = approval_mode
        self.session_id = session_id
        self.agent_name = agent_name
        self.depth = depth
        self.project_instructions = project_instructions
        self.extra_prompt = extra_prompt
        self.mcp = mcp
        # A plain dict: `ToolOutStore` was a dict with a wrapper and a spill-to-disk
        # TODO that never had data to evict. Whatever `elide` cuts is parked here, and
        # `_share_output` copies it into the REPL's `tool_out` so the model can reach it.
        self.outputs: MutableMapping[str, Any] = outputs if outputs is not None else {}
        self.on_progress = on_progress
        self.on_approval = on_approval
        self.on_ask = on_ask
        # One event, shared by every `Runtime` this harness hands out, so a single
        # `set()` reaches all the tools currently in flight. `Runtime` has watched a
        # cancellation flag since it was written; nothing ever set it (EXPECTED.md §14
        # item 4), because a per-call event nobody keeps a reference to cannot be.
        self.cancel = cancel if cancel is not None else asyncio.Event()

    # ---- construction ----

    @classmethod
    async def create(
        cls,
        cwd: str | Path | None = None,
        *,
        approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE,
        load_skills: bool = True,
        load_mcp: bool = True,
        load_git: bool = True,
        load_repl: bool = True,
        mcp_config: str | Path | None = None,
        **kwargs: Any,
    ) -> "Harness":
        """Build a harness with skills discovered and MCP servers connected.

        Async because connecting to MCP servers is. Both discovery steps are optional
        and both fail soft: a broken skill or an unreachable server is reported on the
        registry rather than raised, because neither is a reason a coding session
        cannot start.
        """
        root = Path(cwd).expanduser() if cwd else Path.cwd()
        context = kwargs.pop("context", None) or HarnessContext(cwd=root)
        context.cwd = root
        if load_skills and context.skills is None:
            context.skills = SkillRegistry.discover(root)
        if load_git and not context.git:
            context.git = await git_context(root)
        if load_repl and context.repl is None:
            # Constructed, not started: the subprocess is spawned by the first cell.
            # Most sessions never run one, and paying for an interpreter they will not
            # use is a cost with no matching benefit.
            context.repl = PyREPL(cwd=root, env=context.env)

        kwargs.setdefault("project_instructions", read_project_instructions(root))
        harness = cls(context, approval_mode=approval_mode, **kwargs)

        if load_mcp:
            servers = load_mcp_config(mcp_config, cwd=root)
            if servers:
                manager = MCPManager()
                await manager.connect_all(servers)
                harness.mcp = manager
                harness.registry.extend(manager.tools.values(), replace=True)
                # Registering is not advertising: `allowed` is a fixed list, so an MCP
                # tool nobody adds to it is connected and invisible.
                harness.allow(*manager.tools)
        return harness

    def allow(self, *names: str) -> None:
        """Add tools to what this agent may see. No-op when it may already see everything."""
        if self.allowed is None:
            return
        self.allowed += [name for name in names if name not in self.allowed]

    def for_subagent(
        self,
        name: str,
        *,
        tools: Sequence[str] | None = None,
        scope: str | Sequence[str] | None = None,
        approval_mode: ApprovalMode | None = None,
    ) -> "Harness":
        """A narrowed harness for a child agent.

        The child shares the parent's tool registry, MCP connections, and output store —
        those are session-wide and expensive — but gets its own `HarnessContext`, so its
        write scope, its read tracking, and its todo list are genuinely separate. A child
        that shared the parent's context could scope-check against the parent's paths,
        which would make rule 4 unenforceable.
        """
        scopes = (scope,) if isinstance(scope, str) else tuple(scope or ())
        child_context = HarnessContext(
            cwd=self.context.cwd,
            scope=tuple(self.context.resolve(entry) for entry in scopes),
            skills=self.context.skills,
            session_ctx=self.context.session_ctx,
            env=dict(self.context.env),
        )
        child = Harness(
            child_context,
            allowed=list(tools) if tools is not None else list(WORKER_TOOLS),
            approval_mode=approval_mode or self.approval_mode,
            session_id=self.session_id,
            agent_name=name,
            depth=self.depth + 1,
            project_instructions=self.project_instructions,
            extra_prompt=self.extra_prompt,
            outputs=self.outputs,
            on_progress=self.on_progress,
            on_approval=self.on_approval,
            on_ask=self.on_ask,
            mcp=self.mcp,
            # Interrupting the parent must reach into its children: a sub-agent still
            # editing files after the user pressed Esc is the orphan §10 warns about.
            cancel=self.cancel,
        )
        child.registry = self.registry
        return child

    # ---- what the model sees ----

    def tool_names(self) -> list[str]:
        return [entry.name for entry in self.registry.select(self.allowed, approval_mode=self.approval_mode)]

    def tool_definitions(self, names: Sequence[str] | None = None) -> list[ToolDefinition]:
        """The tool list for a turn, filtered by this agent's allowance and its mode."""
        return self.registry.definitions(
            names if names is not None else self.allowed, approval_mode=self.approval_mode
        )

    def system_prompt(self, *, mode: PromptMode | None = None, task: str = "") -> str:
        """This agent's system prompt.

        `subagent` is derived from `depth` rather than passed in: a harness made by
        `for_subagent()` is a sub-agent by construction, and a caller who could say
        otherwise is a caller who will eventually get it wrong.
        """
        skills = self.context.skills
        return build_system_prompt(
            mode or mode_for(self.approval_mode),
            subagent=self.depth > 0,
            cwd=str(self.context.cwd),
            approval_mode=self.approval_mode,
            tool_names=self.tool_names(),
            skill_catalogue=skills.catalogue() if skills else "",
            project_instructions=self.project_instructions,
            write_scope=", ".join(str(path) for path in self.context.scope),
            todos=self.context.render_todos() if self.context.todos else "",
            git=self.context.git,
            extra="\n\n".join(part for part in (self.extra_prompt, task) if part),
        )

    # ---- running a tool ----

    def runtime(self) -> Runtime[HarnessContext]:
        """A fresh runtime carrying this harness's policy and callbacks.

        Built per call rather than cached: `Runtime` holds an `execution_id`, a start
        time, and a cancellation flag, and sharing one across concurrent tool calls
        mislabels every log line they produce.
        """
        return Runtime(
            context=self.context,
            cancel=self.cancel,
            session_id=self.session_id,
            agent_name=self.agent_name,
            depth=self.depth,
            approval_mode=self.approval_mode,
            outputs=self.outputs,
            # Rule 1's proviso, decided in exactly one place: an elision may only name
            # `tool_out[...]` if there is a REPL holding it.
            outputs_reachable=getattr(self.context, "repl", None) is not None,
            on_progress=self.on_progress,
            on_approval=self.on_approval,
            on_ask=self.on_ask,
        )

    async def invoke(
        self, name: str, arguments: dict[str, Any] | None = None, *, tool_call_id: str = ""
    ) -> ToolResult:
        """Run one model-issued tool call. Never raises for a tool-level failure.

        An unknown or disallowed name comes back as an error result too, rather than an
        exception: the model chose that name, so the model is who needs to hear about it.
        """
        allowed = {entry.name for entry in self.registry.select(self.allowed, approval_mode=self.approval_mode)}
        if name not in allowed:
            available = ", ".join(sorted(allowed))
            return ToolResult.error(
                f"`{name}` is not available to this agent. You have: {available}.", tool=name
            )
        result = await self.registry[name].invoke(arguments, self.runtime(), tool_call_id=tool_call_id)
        await self._share_output(result)
        return result

    async def _share_output(self, result: ToolResult) -> None:
        """Push an elided result's full payload across to the REPL's `tool_out`.

        This is the half of rule 1 that makes eliding non-lossy: the model was just told
        the rest is in `tool_out["..."]`, and this is what puts it there. Failing to
        inject is not worth failing the tool call over — the model has the elided view
        either way — so a dead REPL costs a hint, not a turn.
        """
        repl = getattr(self.context, "repl", None)
        if repl is None or not result.output_id:
            return
        with contextlib.suppress(Exception):
            await repl.inject({result.output_id: self.outputs.get(result.output_id)})

    # ---- ambient runtime ----

    @contextlib.contextmanager
    def bind(self) -> Iterator[Runtime[HarnessContext]]:
        """Make this harness's runtime the ambient one for the block.

        Lets in-process code — a test, a REPL cell running in this interpreter — call
        `await read("x.py")` without threading a runtime through. A context variable
        rather than a global because sub-agents run concurrently and each needs its own.
        """
        runtime = self.runtime()
        token = current_runtime.set(runtime)
        try:
            yield runtime
        finally:
            current_runtime.reset(token)

    # ---- lifecycle ----

    async def aclose(self) -> None:
        """Release MCP connections and kill anything still running in the background.

        §8: Ctrl-C with five sub-agents in flight must leave no orphans. Background
        shells are this layer's share of that promise.
        """
        for shell in list(self.context.shells.values()):
            if shell.running:
                with contextlib.suppress(ProcessLookupError, OSError):
                    shell.process.kill()
            if shell.pump is not None:
                shell.pump.cancel()
        self.context.shells.clear()
        if self.context.repl is not None:
            await self.context.repl.aclose()
        if self.mcp is not None:
            await self.mcp.aclose()

    async def __aenter__(self) -> "Harness":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


def read_project_instructions(root: Path) -> str:
    """The repository's own guidance for agents working in it, if it has any."""
    for name in PROJECT_INSTRUCTION_FILES:
        candidate = root / name
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


__all__ = ["PROJECT_INSTRUCTION_FILES", "Harness", "read_project_instructions"]
