"""
Tool base — what `@tool` is, and what a tool is handed when it runs.

Three ideas, and everything else here serves one of them.

**A Python function is already a tool.** Its name, its parameters, their types and
defaults, and its docstring are a complete description of a call. `@tool` reads that
description rather than asking for it a second time in schema form (`schema.py` does
the reading), so a tool's interface cannot drift from its implementation — there is
only one copy of it.

**`Runtime` is the parameter the model cannot see.** A tool needs things that are not
arguments: where the session is rooted, which agent is calling, whether the human has
approved writes, how to report progress, where to stash a 4 MB result. Threading those
through as ordinary arguments would put them in the JSON Schema, and a model that can
see `approval_mode` is a model that will try to set it. A parameter annotated
`Runtime[T]` is filtered out of the schema and injected at call time instead. `T` is
the agent's own context type, so a tool that needs `HarnessContext` says so and type-checks.

**A tool never decides its own permissions.** It declares a `ToolPermission` once, at
definition. Whether that class runs unattended under the current mode is
`harness/approvals.py`'s call, applied uniformly in `invoke()` — see that module for
why the decision cannot live in the tools.

    @tool(permission=ToolPermission.WRITE)
    async def write(path: str, content: str, runtime: Runtime[HarnessContext]) -> str:
        '''Write `content` to `path`, creating parent directories as needed.'''
        ...

    write.to_tool_definition()          # -> ToolDefinition, ready for a provider
    await write.invoke({"path": ...}, runtime=rt)   # gated, validated, never raises
    await write(path, content, runtime=rt)          # plain call, raises like Python
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    MutableMapping,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from pydantic import BaseModel, Field, ValidationError

from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.harness.approvals import (
    DEFAULT_APPROVAL_MODE,
    ApprovalMode,
    ToolPermission,
    is_forbidden,
    requires_approval,
)
from stcode.core.harness.errors import (
    ToolCancelled,
    ToolDenied,
    ToolError,
    ToolForbidden,
    ToolTimeout,
)
from stcode.core.harness.tools.schema import build_params_model, json_schema_for, split_docstring
from stcode.core.common.truncate import (
    DEFAULT_VIEW_LIMIT,
    NARROW_REQUEST_HINT,
    elide,
    tool_out_hint,
)

CtxT = TypeVar("CtxT")

DEFAULT_MAX_OUTPUT = DEFAULT_VIEW_LIMIT
"""Chars of a tool's result the model sees (CLAUDE.md §4 rule 1). Tools whose whole job
is bulk retrieval raise it explicitly."""

logger = logging.getLogger("stcode.harness.tools")


# ---- the human in the loop -----------------------------------------------------


class ApprovalRequest(BaseModel):
    """What the UI is asked to show when a tool needs a human's yes.

    Carries the validated arguments rather than a pre-rendered sentence: the CLI knows
    how to show a diff for `edit` and a command line for `bash`, and `cli/labels.py`
    owns that wording. The engine's job is to say what is about to happen, not how it
    reads (CLAUDE.md §3).
    """

    tool_name: str
    permission: ToolPermission
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_name: str = "main"
    execution_id: str = ""


class Question(BaseModel):
    """A question put to the user mid-turn by `ask_user_question`."""

    question: str
    header: str = ""
    options: list[str] = Field(default_factory=list)
    multi_select: bool = False


ProgressFn = Callable[[str], Any]
"""`(text) -> None | Awaitable[None]`. Streams a line of progress to the UI."""

ApprovalFn = Callable[[ApprovalRequest], Awaitable[bool]]
AskFn = Callable[[Question], Awaitable[str]]


# ---- runtime -------------------------------------------------------------------


@dataclass
class Runtime(Generic[CtxT]):
    """Everything a tool needs that is not an argument.

    Injected, never advertised. The fields fall into four groups, and the grouping is
    the point — mixing them is what makes agent plumbing unreadable:

    * **`context`** — the agent's own state, typed by the caller. Built-in tools use
      `HarnessContext` (cwd, path scope, todos, skills); a different agent can define
      its own and its tools will type-check against that instead.
    * **identity** — who is calling. `session_id`/`agent_name`/`depth` are what the
      trajectory log groups by, and §9 is explicit that debuggability has to be
      designed in: agent spawns agent spawns tool leaves no natural stack, so the
      lineage has to be carried explicitly or it does not exist.
    * **this call** — `execution_id` is ours and always present; `tool_call_id` is the
      provider's `tool_use` id and is empty when the tool was called from the REPL
      rather than from a model. `attempt` lets a tool behave differently on a retry.
    * **capabilities** — the approval mode in force, the cancellation flag, the
      deadline, and the three callbacks a tool may reach for (progress, approval,
      asking the user). All optional: a headless run wires none of them and tools that
      need one get a clear failure rather than a hang.
    """

    context: CtxT

    session_id: str = ""
    agent_name: str = "main"
    agent_id: str = ""
    depth: int = 0

    tool_name: str = ""
    tool_call_id: str = ""
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attempt: int = 1
    started_at: float = field(default_factory=time.monotonic)

    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE
    deadline: float | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    outputs: MutableMapping[str, Any] = field(default_factory=dict)
    """The in-process mirror of the REPL's `tool_out`. Anything elided out of a result
    is still here under the result's `output_id`; `Harness.invoke` pushes it across. A
    plain dict by default so a tool is testable without a REPL."""

    outputs_reachable: bool = False
    """Whether a REPL is attached, so the model can actually read `tool_out`.

    This is the whole of rule 1's proviso, as a boolean. True and an elision may name
    `tool_out["..."]`; False and it may only say "ask for a narrower range". Set by
    `Harness.runtime()`, never guessed here."""

    on_progress: ProgressFn | None = None
    on_approval: ApprovalFn | None = None
    on_ask: AskFn | None = None

    logger: logging.Logger = logger
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- derived ----

    @property
    def remaining(self) -> float | None:
        """Seconds left before `deadline`, or None when the call is untimed."""
        if self.deadline is None:
            return None
        return max(self.deadline - time.monotonic(), 0.0)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def raise_if_cancelled(self) -> None:
        """Cooperative cancellation checkpoint for long loops inside a tool.

        §8 asks that Ctrl-C with five sub-agents in flight leave no orphans and no
        half-written files. `asyncio.CancelledError` handles the awaits; this handles
        the stretches between them, where a tool is busy in Python and unreachable.
        """
        if self.cancel.is_set():
            raise ToolCancelled(f"{self.tool_name or 'tool'} was cancelled")

    def for_tool(self, tool_name: str, *, tool_call_id: str = "", attempt: int = 1) -> "Runtime[CtxT]":
        """A copy scoped to one specific invocation.

        Copied rather than mutated because sibling tools genuinely do run concurrently
        (that is the whole of §6's turn 3), and a shared runtime whose `tool_name`
        changes underneath them mislabels every log line they emit.
        """
        clone = Runtime(context=self.context)
        for slot, value in vars(self).items():
            setattr(clone, slot, value)
        clone.tool_name = tool_name
        clone.tool_call_id = tool_call_id
        clone.attempt = attempt
        clone.execution_id = uuid.uuid4().hex[:12]
        clone.started_at = time.monotonic()
        return clone

    # ---- capabilities ----

    async def progress(self, text: str) -> None:
        """Report a line of progress. A no-op when nothing is listening."""
        if self.on_progress is None:
            return
        outcome = self.on_progress(text)
        if inspect.isawaitable(outcome):
            await outcome

    async def request_approval(self, permission: ToolPermission, arguments: dict[str, Any]) -> bool:
        """Ask the human to approve this call, per the mode in force.

        Returns True when the tool may proceed. With no approval callback wired, a call
        that needs one is refused rather than allowed: a headless run that silently
        upgrades itself to `full-auto` is exactly the failure §9 says never to have.
        """
        if is_forbidden(self.approval_mode, permission):
            raise ToolForbidden(
                f"`{self.tool_name}` needs {permission.value} access, which the "
                f"{self.approval_mode!r} approval mode does not allow. Switch modes with "
                "/mode, or use a tool that only reads."
            )
        if not requires_approval(self.approval_mode, permission):
            return True
        if self.on_approval is None:
            raise ToolDenied(
                f"`{self.tool_name}` needs approval under the {self.approval_mode!r} mode, "
                "but this session has no way to ask. Run in a mode that permits it."
            )
        return await self.on_approval(
            ApprovalRequest(
                tool_name=self.tool_name,
                permission=permission,
                arguments=arguments,
                agent_name=self.agent_name,
                execution_id=self.execution_id,
            )
        )

    async def ask(self, question: Question) -> str:
        """Put a question to the user and wait for the answer."""
        if self.on_ask is None:
            raise ToolError(
                "There is no user attached to this session to ask. Decide with the "
                "information you have and state the assumption you made."
            )
        return await self.on_ask(question)


current_runtime: contextvars.ContextVar[Runtime[Any] | None] = contextvars.ContextVar(
    "stcode_current_runtime", default=None
)
"""The runtime a bare `await read(...)` picks up.

A context variable rather than a global because sub-agents run concurrently in one
process and each needs its own cwd, scope, and approval mode. `Harness.bind()` sets it
for the duration of a turn; `Tool.bind()` closes over one explicitly when that is
clearer.
"""


# ---- tools ---------------------------------------------------------------------


def _runtime_parameter(fn: Callable[..., Any]) -> str | None:
    """Find the parameter annotated `Runtime` / `Runtime[T]`, if any.

    Matched on the annotation, not the name: a tool is free to call it `rt`, and a tool
    with a plain parameter that happens to be named `runtime` should keep it.
    """
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        annotation = hints.get(name, parameter.annotation)
        if annotation is Runtime or get_origin(annotation) is Runtime:
            return name
        # Annotated[Runtime[Ctx], ...] — unwrap one layer and look again.
        for arg in get_args(annotation):
            if arg is Runtime or get_origin(arg) is Runtime:
                return name
    return None


class Tool(Generic[CtxT]):
    """One callable, presented three ways.

    Built from a decorated Python function in almost every case. The exception is a
    tool whose schema is authored elsewhere (an MCP server's), which is passed in as
    `input_schema` and short-circuits both derivation and validation.

    * `to_tool_definition()` — the schema a provider advertises.
    * `invoke(arguments, runtime)` — the gated, validated path the agent loop uses.
      It reports failures as results and never raises for a tool-level problem, because
      the loop's next move is always to hand a `tool_result` back to the model.
    * `__call__(...)` — an ordinary Python call for REPL and test code, which raises
      like ordinary Python. The runtime comes from the `current_runtime` context
      variable, or from an explicit keyword.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        permission: ToolPermission = ToolPermission.READ,
        timeout: float | None = None,
        max_output: int = DEFAULT_MAX_OUTPUT,
        spill: bool = True,
        permission_for: Callable[[dict[str, Any]], ToolPermission] | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "tool")
        self.permission = permission
        self.timeout = timeout
        self.max_output = max_output
        self.spill = spill
        self.permission_for = permission_for

        prose, _ = split_docstring(fn)
        self.description = description if description is not None else prose
        if not self.description:
            raise ValueError(
                f"tool {self.name!r} has no description: give it a docstring, or pass "
                "description=... . The description is the entire prompt the model reads "
                "for this tool (CLAUDE.md §11)."
            )

        self.runtime_param = _runtime_parameter(fn)
        if input_schema is not None:
            # The schema came from somewhere authoritative — an MCP server describing
            # its own tool — so it is used verbatim and arguments are passed through
            # unvalidated. Re-deriving it from a `**kwargs` shim would throw away the
            # server's types, and validating against a model we invented would reject
            # calls the server would have accepted.
            self.params_model = None
            self.input_schema = input_schema
        else:
            excluded = frozenset({self.runtime_param}) if self.runtime_param else frozenset()
            self.params_model = build_params_model(fn, exclude=excluded)
            self.input_schema = json_schema_for(self.params_model)
        self.is_async = inspect.iscoroutinefunction(fn)

        # Keep the wrapped function introspectable — `help(read)` in the REPL should
        # show the tool's own docstring, not this class's.
        self.__doc__ = inspect.getdoc(fn)
        self.__name__ = self.name
        self.__signature__ = inspect.signature(fn)

    def __repr__(self) -> str:
        return f"<tool {self.name}({', '.join(self.input_schema.get('properties', {}))})>"

    def to_tool_definition(self) -> ToolDefinition:
        """The provider-facing shape. Stable across turns for a given tool, which is
        what keeps it inside the cached prompt prefix (§8)."""
        return ToolDefinition(
            name=self.name, description=self.description, input_schema=self.input_schema
        )

    @property
    def definition(self) -> ToolDefinition:
        return self.to_tool_definition()

    def effective_permission(self, arguments: dict[str, Any]) -> ToolPermission:
        """The permission this *particular* call needs.

        Almost always the declared one. `bash` is the exception that justifies the
        hook: `git status` and `rm -rf build/` arrive through the same tool, and gating
        them identically means either prompting for every `ls` or never prompting at
        all. A tool may narrow the class it claims per call — it may not widen its own
        latitude, since the declared permission is still what the mode was checked
        against when the tool set was assembled.
        """
        if self.permission_for is None:
            return self.permission
        return self.permission_for(arguments)

    # ---- calling ----

    def bind(self, runtime: Runtime[CtxT]) -> Callable[..., Awaitable[Any]]:
        """A plain async callable with `runtime` already supplied.

        This is what gets injected into a kernel namespace, so REPL code reads as
        `await read("src/main.py")` rather than carrying plumbing through every call.
        """

        async def bound(*args: Any, **kwargs: Any) -> Any:
            return await self._call(args, kwargs, runtime)

        bound.__name__ = self.name
        bound.__doc__ = self.__doc__
        return bound

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        runtime = kwargs.pop("runtime", None) or current_runtime.get()
        return await self._call(args, kwargs, runtime)

    async def _call(self, args: tuple[Any, ...], kwargs: dict[str, Any], runtime: Any) -> Any:
        if self.runtime_param is not None:
            if runtime is None:
                raise ToolError(
                    f"`{self.name}` needs a Runtime and none is bound. Call it through "
                    "Harness.invoke(), or bind one with `Harness.bind()` first."
                )
            kwargs[self.runtime_param] = runtime
        outcome = self.fn(*args, **kwargs)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    async def invoke(
        self,
        arguments: dict[str, Any] | None = None,
        runtime: Runtime[CtxT] | None = None,
        *,
        tool_call_id: str = "",
    ) -> ToolResult:
        """Run the tool for a model-issued call: validate, gate, execute, render.

        Never raises for a tool-level failure. Every outcome the model needs to react to
        — bad arguments, a denied approval, a timeout, a bug inside the tool — comes
        back as a `ToolResult` with `is_error` set, because the agent loop's only move
        is to return a `tool_result` block and let the model try something else. A raise
        here would instead take down the turn.
        """
        runtime = runtime or current_runtime.get()  # type: ignore[assignment]
        if runtime is None:
            return ToolResult.error(f"`{self.name}` was invoked with no runtime bound.")
        call = runtime.for_tool(self.name, tool_call_id=tool_call_id)

        if self.params_model is None:
            kwargs = dict(arguments or {})
        else:
            try:
                validated = self.params_model.model_validate(arguments or {})
            except ValidationError as exc:
                return ToolResult.error(
                    f"Invalid arguments for `{self.name}`:\n{_render_validation_error(exc)}",
                    tool=self.name,
                )
            # Attribute access, not `model_dump()`: dumping recursively converts nested
            # models back into dicts, so a tool declaring `todos: list[TodoItem]` would
            # be handed a list of dicts and fail on the first attribute access.
            kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
        try:
            call.raise_if_cancelled()
            if not await call.request_approval(self.effective_permission(kwargs), kwargs):
                raise ToolDenied(
                    f"The user declined to run `{self.name}`. Do not retry it — ask what "
                    "they would prefer, or take a different approach."
                )
            value = await self._run(kwargs, call)
        except ToolError as exc:
            return ToolResult.error(str(exc), tool=self.name, kind=type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A bug inside the tool, not a message for the model. Log the traceback for
            # whoever is reading the trajectory; send back only what is actionable.
            call.logger.exception("tool %s raised", self.name, extra={"tool": self.name})
            return ToolResult.error(f"`{self.name}` failed: {type(exc).__name__}: {exc}", tool=self.name)

        return self._render(value, call)

    async def _run(self, kwargs: dict[str, Any], call: Runtime[CtxT]) -> Any:
        if self.runtime_param is not None:
            kwargs = {**kwargs, self.runtime_param: call}

        if self.is_async:
            coro = self.fn(**kwargs)
        else:
            # §11: no blocking I/O inside the agent loop. A sync tool is assumed to
            # block — that is usually why it is sync — so it runs off the loop thread.
            coro = asyncio.to_thread(self.fn, **kwargs)

        budget = self.timeout if self.timeout is not None else call.remaining
        try:
            if budget is None:
                return await coro
            return await asyncio.wait_for(coro, budget)
        except asyncio.TimeoutError:
            raise ToolTimeout(
                f"`{self.name}` exceeded its {budget:.0f}s budget. Narrow the request "
                "(a smaller path, a tighter pattern) or raise the timeout."
            ) from None

    def _render(self, value: Any, call: Runtime[CtxT]) -> ToolResult:
        """Turn whatever the function returned into what the model reads.

        A tool that already knows how it wants to be seen returns a `ToolResult` and is
        passed straight through; everything else is stringified, and anything over
        `max_output` is elided.

        The elision marker names `tool_out["..."]` only when a REPL is attached to
        carry the payload across, and otherwise says to re-call with a narrower range.
        Rule 1 permits exactly one hint: a true one.
        """
        if isinstance(value, ToolResult):
            result = value
        else:
            result = ToolResult(content=_stringify(value), payload=value)

        if result.is_error or len(result.content) <= self.max_output:
            return result

        hint = NARROW_REQUEST_HINT
        if self.spill:
            output_id = result.output_id or f"{self.name}_{call.execution_id}"
            call.outputs[output_id] = result.payload if result.payload is not None else result.content
            result.output_id = output_id
            if call.outputs_reachable:
                hint = tool_out_hint(output_id)
        result.content = elide(result.content, self.max_output, hint=hint)
        return result


def _stringify(value: Any) -> str:
    if value is None:
        return "(no output)"
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _render_validation_error(exc: ValidationError) -> str:
    """Pydantic's default rendering names a model the tool author invented and the model
    has never heard of. Report the argument instead."""
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(arguments)"
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)


# ---- the decorator -------------------------------------------------------------


@overload
def tool(fn: Callable[..., Any], /) -> Tool[Any]: ...


@overload
def tool(
    *,
    name: str | None = ...,
    description: str | None = ...,
    permission: ToolPermission = ...,
    timeout: float | None = ...,
    max_output: int = ...,
    spill: bool = ...,
    permission_for: Callable[[dict[str, Any]], ToolPermission] | None = ...,
) -> Callable[[Callable[..., Any]], Tool[Any]]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    permission: ToolPermission = ToolPermission.READ,
    timeout: float | None = None,
    max_output: int = DEFAULT_MAX_OUTPUT,
    spill: bool = True,
    permission_for: Callable[[dict[str, Any]], ToolPermission] | None = None,
) -> Tool[Any] | Callable[[Callable[..., Any]], Tool[Any]]:
    """Turn a function into a tool. Usable bare (`@tool`) or configured (`@tool(...)`).

    Args:
        name: Overrides the function's name. The model sees this, so prefer renaming
            the function — a tool whose advertised name and Python name disagree is a
            grep away from confusing whoever reads the trajectory next.
        description: Overrides the docstring. Same caveat: the docstring is the prompt
            (CLAUDE.md §11), and keeping it next to the code is why that works.
        permission: The class of side effect this tool has, which decides whether it
            needs a human's yes under the current mode. Defaults to `READ` — the
            conservative direction is to under-claim capability, since an under-claimed
            tool asks too often while an over-claimed one writes without asking.
        timeout: Seconds before the call is abandoned. `None` inherits the runtime's
            remaining deadline, so a tool with no opinion still cannot outlive its turn.
        max_output: Chars of result the model sees before elision. Raise it for tools
            whose entire job is bulk retrieval; the elided remainder is still in
            `tool_out` either way.
        spill: Whether an over-long result is parked in `tool_out`. Off only for tools
            whose output is worthless once truncated (a progress echo, a confirmation).
        permission_for: Narrows `permission` per call, given the validated arguments.
            For tools like `bash` whose risk lives in the argument rather than the tool.
    """

    def decorate(target: Callable[..., Any]) -> Tool[Any]:
        return Tool(
            target,
            name=name,
            description=description,
            permission=permission,
            timeout=timeout,
            max_output=max_output,
            spill=spill,
            permission_for=permission_for,
        )

    return decorate(fn) if fn is not None else decorate


def to_tool_definition(target: Tool[Any] | Callable[..., Any]) -> ToolDefinition:
    """The `ToolDefinition` for a tool, or for a plain function that was never decorated.

    Accepting an undecorated callable is deliberate: MCP-backed and dynamically
    generated tools arrive as functions, and the schema derivation is the same work
    either way.
    """
    if isinstance(target, Tool):
        return target.to_tool_definition()
    return Tool(target).to_tool_definition()


__all__ = [
    "ApprovalFn",
    "ApprovalRequest",
    "AskFn",
    "DEFAULT_MAX_OUTPUT",
    "ProgressFn",
    "Question",
    "Runtime",
    "Tool",
    "ToolCancelled",
    "ToolDenied",
    "ToolError",
    "ToolForbidden",
    "ToolPermission",
    "ToolResult",
    "ToolTimeout",
    "current_runtime",
    "to_tool_definition",
    "tool",
]
