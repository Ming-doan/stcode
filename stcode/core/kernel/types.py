"""
Kernel Types — the shape of a single REPL execution.

`ExecResult` always carries the *full* output. Truncation is a rendering concern and
lives in `view()` / `truncate.py`, so the caller that needs everything (trajectory log,
TUI scrollback) still has it while the agent sees a capped render.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from stcode.core.kernel.truncate import DEFAULT_VIEW_LIMIT, elide

_ERROR_BUDGET = 4096
"""Chars reserved for a traceback before stdout gets what's left. Errors are the most
information-dense thing a cell can produce; they must never be pushed out by a print."""

_RESULT_BUDGET = 1024
"""Chars reserved for the cell's final expression repr."""


class ExecOutcome(StrEnum):
    """How an execution ended.

    `TIMEOUT` and `INTERRUPTED` are distinct on purpose: the first is us giving up on a
    cell, the second is a user or parent cancelling one. The agent should retry
    differently in each case.
    """

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    DEAD = "dead"


class KernelError(BaseModel):
    """An uncaught exception from the kernel, ANSI already stripped."""

    ename: str
    evalue: str
    traceback: str = ""

    def render(self) -> str:
        return self.traceback or f"{self.ename}: {self.evalue}"


class ExecResult(BaseModel):
    """The complete result of one `execute()` — untruncated.

    `stdout`/`stderr` are the concatenated stream messages; `result_repr` is the
    `execute_result` (a cell's trailing expression); `displays` holds the text/plain of
    any `display_data` (a `display()` call, a rich repr).
    """

    outcome: ExecOutcome
    stdout: str = ""
    stderr: str = ""
    result_repr: str | None = None
    displays: list[str] = Field(default_factory=list)
    error: KernelError | None = None
    execution_count: int | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is ExecOutcome.OK

    def view(self, limit: int = DEFAULT_VIEW_LIMIT) -> str:
        """Render for the agent's context, capped at `limit` chars.

        Budgets rather than truncating the concatenation: the traceback and the final
        expression are allocated first, and whatever remains goes to the streams. A cell
        that prints 400 KB and then raises still shows its exception.
        """
        error_text = elide(self.error.render(), min(_ERROR_BUDGET, limit)) if self.error else ""
        result_text = elide(self.result_repr, min(_RESULT_BUDGET, limit)) if self.result_repr else ""

        body = self.stdout + "".join(f"\n{d}" for d in self.displays)
        reserved = len(error_text) + len(result_text) + 64
        budget = max(limit - reserved, 512)
        if self.stderr and body:
            stderr_budget = min(len(self.stderr), max(budget // 3, 256))
        else:
            stderr_budget = budget
        stdout_budget = budget - (stderr_budget if self.stderr else 0)

        parts: list[str] = []
        if body:
            parts.append(elide(body, stdout_budget, hint="_stdout"))
        if self.stderr:
            parts.append("[stderr]\n" + elide(self.stderr, stderr_budget, hint="_stderr"))
        if result_text:
            parts.append(f"[out] {result_text}")
        if error_text:
            parts.append(error_text)

        if not parts:
            return "" if self.ok else f"[{self.outcome}]"
        return "\n".join(parts)
