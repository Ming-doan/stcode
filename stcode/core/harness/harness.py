"""
Harness — the one object the agent loop talks to.

    harness = await Harness.create(cwd=project_root, approval_mode="auto-edit")
    system  = harness.system_prompt()
    tools   = harness.tool_definitions()
    result  = await harness.invoke("read", {"path": "src/main.py"}, tool_call_id=call.id)

Assembles everything under `core/harness/`: tools, skills, MCP servers, the prompt, and
the `Runtime` that carries them into a call. The agent loop needs two verbs from here —
"what do I advertise" and "run this" — and nothing about how either is built.

A `Harness` is **per agent**, not per process: its context holds a cwd, a write scope
and an approval mode, and two sub-agents must be able to have different scopes at once.
`for_subagent()` makes the narrowed copy; mutating one harness between spawns is the
bug that rule exists to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Iterator, MutableMapping, Sequence

from stcode.core.common import trace
from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.harness.context import HarnessContext, git_context
from stcode.core.harness.mcp import (
    MCP_CODE_DIRNAME,
    MCPManager,
    generate_server_code,
    load_mcp_config,
)
from stcode.core.harness.outputs import OutputStore
from stcode.core.harness.prompts import PromptMode, build_system_prompt, load_role, mode_for
from stcode.core.harness.skills import SkillRegistry
from stcode.core.harness.tools import BUILTIN_TOOLS, MAIN_TOOLS, WORKER_TOOLS, ToolRegistry
from stcode.core.harness.tools.base import (
    ApprovalFn,
    AskFn,
    EventFn,
    ProgressFn,
    Runtime,
    Tool,
    current_runtime,
)
from stcode.core.repl import PyREPL

PROJECT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".stcode/instructions.md")
"""Read in order; first hit wins. `AGENTS.md` is the cross-agent convention, and a repo
that has one has already written down how it wants to be worked in."""


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
        role: str = "",
        teammates: Sequence[str] = (),
        outputs: MutableMapping[str, Any] | None = None,
        mcp_catalogue: str = "",
        on_progress: ProgressFn | None = None,
        on_approval: ApprovalFn | None = None,
        on_ask: AskFn | None = None,
        on_event: EventFn | None = None,
        mcp: MCPManager | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        self.context = context if context is not None else HarnessContext()
        self.registry = ToolRegistry(tools if tools is not None else BUILTIN_TOOLS)
        # An explicit `tools=` list is already the caller's selection, so allow all of
        # it. The built-in set is not — `MAIN_TOOLS` is the narrower default.
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
        self.role = role
        """The role's markdown body, already loaded. A string, not a name: `Harness`
        should not know where roles live on disk."""

        self.teammates = list(teammates)
        self.mcp = mcp
        self.mcp_catalogue = mcp_catalogue
        """Server and tool *names*, for the prompt. Empty in `tools` mode, where the
        definitions are advertised instead."""

        self.mcp_servers: dict[str, list[str]] = {}
        """Server -> tool names, for a client that wants to show what is connected.

        Kept because in `code` mode the manager is closed as soon as it has said what
        each server offers: the connection is gone by then, the facts are not, and
        `/mcp` still has to be able to answer."""
        # Whatever `elide` cuts is parked here; `_share_output` copies it into the
        # REPL's `tool_out` so the model can reach it. Bounded: nothing else would ever
        # remove an entry, and a long session would hold every large result it made.
        self.outputs: MutableMapping[str, Any] = outputs if outputs is not None else OutputStore()
        self.on_progress = on_progress
        self.on_approval = on_approval
        self.on_ask = on_ask
        self.on_event = on_event
        # One event, shared by every `Runtime` this harness hands out, so a single
        # `set()` reaches every tool in flight. A per-call event nobody holds a
        # reference to could never be set at all.
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
        role: str = "",
        mcp_expose: str = "code",
        mcp_config: str | Path | None = None,
        **kwargs: Any,
    ) -> "Harness":
        """Build a harness with skills discovered and MCP servers connected.

        Async because connecting to MCP servers is. Both discovery steps fail soft — a
        broken skill or unreachable server is reported, not raised. Neither is a reason
        a coding session cannot start.
        """
        root = Path(cwd).expanduser() if cwd else Path.cwd()
        if not root.is_dir():
            root = Path.cwd()
        context = kwargs.pop("context", None) or HarnessContext(cwd=root)
        context.cwd = root
        if load_skills and context.skills is None:
            context.skills = SkillRegistry.discover(root)
        if load_git and not context.git:
            context.git = await git_context(root)
        if load_repl and context.repl is None:
            # Constructed, not started: the first cell spawns the subprocess. Most
            # sessions never run one.
            context.repl = PyREPL(cwd=root, env=context.env)

        # Raises on an unknown name: a container started with a typo'd role, or with
        # its agents directory unmounted, should refuse rather than run an agent that
        # owns nothing.
        kwargs.setdefault("role", load_role(role, root))
        kwargs.setdefault("project_instructions", read_project_instructions(root))
        harness = cls(context, approval_mode=approval_mode, **kwargs)

        if load_mcp:
            servers = load_mcp_config(mcp_config, cwd=root)
            if servers:
                manager = MCPManager()
                await manager.open(servers)
                harness.mcp_servers = {
                    server: sorted(tools) for server, tools in manager.schemas().items()
                }
                if mcp_expose == "tools":
                    harness.mcp = manager
                    harness.registry.extend(manager.tools.values(), replace=True)
                    # Registering is not advertising: `allowed` is a fixed list, so an
                    # MCP tool nobody adds to it stays connected and invisible.
                    harness.allow(*manager.tools)
                else:
                    # Code mode: we connected only to ask what each server offers. The
                    # stubs open their own connection inside the REPL, so holding this
                    # one would mean two per server.
                    generate_server_code(root, manager.schemas())
                    harness.mcp_catalogue = manager.catalogue()
                    await manager.aclose()
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

        Shares the parent's tool registry, MCP connections and output store — those are
        session-wide and expensive — but gets its own `HarnessContext`, so its write
        scope, read tracking and todos are genuinely separate. A shared context would
        scope-check against the parent's paths and make the rule unenforceable.
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
            role=self.role,
            teammates=self.teammates,
            outputs=self.outputs,
            # No `mcp_catalogue`: a sub-agent has no `repl`, so this would spend
            # tokens advertising a dead end.
            on_progress=self.on_progress,
            on_approval=self.on_approval,
            on_ask=self.on_ask,
            # A sub-agent gets no `task`, so it never emits; passed anyway so the
            # arrow is one-way and a future nested case does not silently go dark.
            on_event=self.on_event,
            mcp=self.mcp,
            # Interrupting the parent must reach its children: a sub-agent still
            # editing files after Esc is exactly the orphan to avoid.
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

        `subagent` is derived from `depth`, not passed in: a harness from
        `for_subagent()` is a sub-agent by construction, and a caller allowed to say
        otherwise will eventually get it wrong.
        """
        skills = self.context.skills
        return build_system_prompt(
            mode or mode_for(self.approval_mode),
            subagent=self.depth > 0,
            cwd=str(self.context.cwd),
            approval_mode=self.approval_mode,
            tool_names=self.tool_names(),
            skill_catalogue=skills.catalogue() if skills else "",
            role=self.role,
            teammates=self.teammates,
            mcp_catalogue=self.mcp_catalogue,
            mcp_directory=MCP_CODE_DIRNAME,
            project_instructions=self.project_instructions,
            write_scope=", ".join(str(path) for path in self.context.scope),
            todos=self.context.render_todos() if self.context.todos else "",
            git=self.context.git,
            extra="\n\n".join(part for part in (self.extra_prompt, task) if part),
        )

    # ---- running a tool ----

    def runtime(self) -> Runtime[HarnessContext]:
        """A fresh runtime carrying this harness's policy and callbacks.

        Per call, not cached: a `Runtime` holds an `execution_id` and a start time, and
        sharing one across concurrent calls mislabels every log line they produce.
        """
        return Runtime(
            context=self.context,
            cancel=self.cancel,
            session_id=self.session_id,
            agent_name=self.agent_name,
            depth=self.depth,
            approval_mode=self.approval_mode,
            outputs=self.outputs,
            # Decided in exactly one place: an elision may only name `tool_out[...]`
            # if there is a REPL actually holding it.
            outputs_reachable=getattr(self.context, "repl", None) is not None,
            on_progress=self.on_progress,
            on_approval=self.on_approval,
            on_ask=self.on_ask,
            on_event=self.on_event,
        )

    async def invoke(
        self, name: str, arguments: dict[str, Any] | None = None, *, tool_call_id: str = ""
    ) -> ToolResult:
        """Run one model-issued tool call. Never raises for a tool-level failure.

        An unknown or disallowed name comes back as an error result too — the model
        chose that name, so the model is who needs to hear about it.
        """
        allowed = {entry.name for entry in self.registry.select(self.allowed, approval_mode=self.approval_mode)}
        if name not in allowed:
            available = ", ".join(sorted(allowed))
            return ToolResult.error(
                f"`{name}` is not available to this agent. You have: {available}.", tool=name
            )
        # `execute_tool <name>` — the GenAI convention's name for this span, so a
        # platform files it next to the model call that asked for it.
        with trace.span(
            f"execute_tool {name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.call.id": tool_call_id,
                "stcode.agent": self.agent_name,
            },
        ) as recorder:
            result = await self.registry[name].invoke(
                arguments, self.runtime(), tool_call_id=tool_call_id
            )
            recorder.set(**{"stcode.tool.ok": not result.is_error})
            await self._share_output(result)
            return result

    async def _share_output(self, result: ToolResult) -> None:
        """Push an elided result's full payload into the REPL's `tool_out`.

        This is what makes eliding non-lossy: the model was just told the rest is in
        `tool_out["..."]`, and this puts it there. A dead REPL costs a hint, not a turn.
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

        Lets in-process code — a test, a cell in this interpreter — call
        `await read("x.py")` without threading a runtime through.
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

        Ctrl-C with sub-agents in flight must leave no orphans; background shells are
        this layer's share of that.
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
