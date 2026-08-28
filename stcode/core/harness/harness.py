"""
Harness — the one object the agent loop talks to.

Everything under `core/harness/` is assembled here: the tools an agent may use, the
skills it can pull in, the MCP servers it is connected to, the prompt it wakes up with,
and the `Runtime` that carries all of it into a tool call. The agent loop should need
exactly two verbs from this module — "what tools do I advertise" and "run this call" —
and know nothing about how either is put together.

    harness = await Harness.create(cwd=project_root, approval_mode="auto-edit")
    system  = harness.system_prompt(role="worker")
    tools   = harness.tool_definitions()
    result  = await harness.invoke("read", {"path": "src/main.py"}, tool_call_id=call.id)

One decision worth naming: a `Harness` is **per agent**, not per process. Its context
carries a working directory, a write scope, and an approval mode, and CLAUDE.md §2.2
rule 4 depends on two sub-agents having *different* write scopes at the same time.
`for_subagent()` is the supported way to make the narrowed copy — sharing one harness
and mutating its scope between spawns is exactly the bug that rule exists to prevent.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator, MutableMapping, Sequence

from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.mcp import MCPManager, load_mcp_config
from stcode.core.harness.prompts import AgentRole, PromptMode, build_system_prompt, mode_for
from stcode.core.harness.registry import ToolRegistry
from stcode.core.harness.skills import SkillRegistry
from stcode.core.harness.tools import BUILTIN_TOOLS, WORKER_TOOLS
from stcode.core.harness.tools.base import (
    ApprovalFn,
    AskFn,
    ProgressFn,
    Runtime,
    Tool,
    current_runtime,
)
from stcode.core.kernel.store import ToolOutStore

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
    ) -> None:
        self.context = context if context is not None else HarnessContext()
        self.registry = ToolRegistry(tools if tools is not None else BUILTIN_TOOLS)
        self.allowed = list(allowed) if allowed is not None else None
        self.approval_mode = approval_mode
        self.session_id = session_id
        self.agent_name = agent_name
        self.depth = depth
        self.project_instructions = project_instructions
        self.extra_prompt = extra_prompt
        self.mcp = mcp
        # The store from `kernel/store.py`, which was written for exactly this and had
        # nothing to hold until now. Its spill-to-disk policy applies to every oversized
        # tool result without any tool having to know about it.
        self.outputs: MutableMapping[str, Any] = outputs if outputs is not None else ToolOutStore()
        self.on_progress = on_progress
        self.on_approval = on_approval
        self.on_ask = on_ask

    # ---- construction ----

    @classmethod
    async def create(
        cls,
        cwd: str | Path | None = None,
        *,
        approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE,
        load_skills: bool = True,
        load_mcp: bool = True,
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

        kwargs.setdefault("project_instructions", read_project_instructions(root))
        harness = cls(context, approval_mode=approval_mode, **kwargs)

        if load_mcp:
            servers = load_mcp_config(mcp_config, cwd=root)
            if servers:
                manager = MCPManager()
                await manager.connect_all(servers)
                harness.mcp = manager
                harness.registry.extend(manager.tools.values(), replace=True)
        return harness

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

    def system_prompt(
        self,
        *,
        role: AgentRole = "worker",
        mode: PromptMode | None = None,
        task: str = "",
    ) -> str:
        skills = self.context.skills
        return build_system_prompt(
            mode or mode_for(self.approval_mode),
            role=role,
            cwd=str(self.context.cwd),
            approval_mode=self.approval_mode,
            tool_names=self.tool_names(),
            skill_catalogue=skills.catalogue() if skills else "",
            project_instructions=self.project_instructions,
            write_scope=", ".join(str(path) for path in self.context.scope),
            todos=self.context.render_todos() if self.context.todos else "",
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
            session_id=self.session_id,
            agent_name=self.agent_name,
            depth=self.depth,
            approval_mode=self.approval_mode,
            outputs=self.outputs,
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
        return await self.registry[name].invoke(arguments, self.runtime(), tool_call_id=tool_call_id)

    # ---- REPL integration ----

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

    def namespace(self) -> dict[str, Any]:
        """Bound tools, callable with no plumbing: `await namespace["read"]("main.py")`.

        These are live Python objects, so they work in an **in-process** REPL and in
        tests. `PythonKernel` runs in a separate process and cannot receive them by
        assignment — turning `bootstrap.py`'s placeholders into working calls over there
        needs an RPC bridge that marshals each call back to this side. That bridge is
        the agent loop's job (`core/agent/`), and this dict is the tool table it will
        dispatch against.
        """
        runtime = self.runtime()
        bound: dict[str, Any] = {
            entry.name: entry.bind(runtime)
            for entry in self.registry.select(self.allowed, approval_mode=self.approval_mode)
        }
        bound["session_ctx"] = self.context.session_ctx
        bound["tool_out"] = self.outputs
        return bound

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
