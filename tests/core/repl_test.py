"""
REPL tests — a real subprocess every time.

Mocking the worker would test our beliefs about pipes rather than the behaviour that
matters, and the behaviour that matters is exactly the awkward part: what survives an
interrupt, what happens to a cell that ignores one, and whether the reply you get back
belongs to the call you made.

One event loop for the module: subprocess transports bind to the loop that created them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator

import pytest

from stcode.core.common.truncate import tool_out_hint
from stcode.core.harness import Harness, HarnessContext
from stcode.core.repl import ExecResult, PyREPL

@pytest.fixture
def repl(loop: asyncio.AbstractEventLoop) -> Iterator[PyREPL]:
    backend = PyREPL()
    try:
        yield backend
    finally:
        loop.run_until_complete(backend.aclose())


# ---- the namespace -------------------------------------------------------------


def test_the_namespace_persists_between_cells(repl: PyREPL, run: Any) -> None:
    run(repl.execute("value = 41"))
    assert run(repl.execute("print(value + 1)")).stdout.strip() == "42"


def test_top_level_await_works(repl: PyREPL, run: Any) -> None:
    result = run(repl.execute("import asyncio\nawait asyncio.sleep(0)\nprint('awaited')"))
    assert result.ok and result.stdout.strip() == "awaited"


def test_starting_is_lazy(repl: PyREPL, run: Any) -> None:
    """Most sessions never call `repl`. They should not pay for an interpreter."""
    assert not repl.running
    run(repl.execute("pass"))
    assert repl.running


def test_an_exception_is_an_outcome_not_a_crash(repl: PyREPL, run: Any) -> None:
    result = run(repl.execute("1 / 0"))
    assert not result.ok and result.outcome == "error"
    assert "ZeroDivisionError" in (result.error or "")
    assert run(repl.execute("print('still alive')")).ok


# ---- tool_out, the point of the whole thing ------------------------------------


def test_inject_puts_a_value_where_the_elision_hint_says_it_is(repl: PyREPL, run: Any) -> None:
    run(repl.inject({"read_ab12": "the remainder"}))
    assert run(repl.execute("print(tool_out['read_ab12'])")).stdout.strip() == "the remainder"


def test_injecting_before_start_does_not_spawn_an_interpreter(repl: PyREPL, run: Any) -> None:
    """A big tool result is not a reason to pay for a Python the session may never use.
    The value waits, and lands when something actually starts the worker."""
    run(repl.inject({"early": "held"}))
    assert not repl.running
    assert run(repl.execute("print(tool_out['early'])")).stdout.strip() == "held"


# ---- interrupts ----------------------------------------------------------------


def test_a_timeout_interrupts_the_cell_and_keeps_the_namespace(repl: PyREPL, run: Any) -> None:
    run(repl.execute("kept = 'survivor'"))
    result = run(repl.execute("while True: pass", timeout=1.0))
    assert not result.ok and result.outcome == "timeout"
    assert "namespace survived" in (result.error or "")
    assert run(repl.execute("print(kept)")).stdout.strip() == "survivor"


def test_a_cell_that_ignores_sigint_is_killed_and_the_namespace_is_gone(
    repl: PyREPL, run: Any
) -> None:
    run(repl.execute("kept = 'survivor'"))
    result = run(
        repl.execute(
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "time.sleep(30)",
            timeout=1.0,
        )
    )
    assert result.outcome == "timeout" and "killed" in (result.error or "")
    assert run(repl.execute("print('kept' in dir())")).stdout.strip() == "False"


def test_a_late_answer_is_not_handed_to_the_next_call(repl: PyREPL, run: Any) -> None:
    """The desync worth having a test for. A cell that answers its SIGINT emits a
    result frame after we stopped waiting; without id matching, every reply from then
    on belongs to the previous call."""
    run(repl.execute("while True: pass", timeout=1.0))
    assert run(repl.execute("print('mine')")).stdout.strip() == "mine"
    assert run(repl.execute("print('also mine')")).stdout.strip() == "also mine"


# ---- streaming -----------------------------------------------------------------


def test_output_streams_line_by_line_while_the_cell_runs(repl: PyREPL, run: Any) -> None:
    seen: list[str] = []

    async def on_stream(name: str, text: str) -> None:
        seen.append(text)

    run(repl.execute("for i in range(3):\n    print('line', i)", on_stream=on_stream))
    assert seen == ["line 0", "line 1", "line 2"]


# ---- the view the model reads --------------------------------------------------


def test_the_view_elides_and_keeps_both_ends() -> None:
    result = ExecResult(stdout="A" * 200 + "\n" + "Z" * 200)
    view = result.view(120)
    assert len(view) <= 120 and view.startswith("A") and view.endswith("Z")
    assert "elided" in view


# ---- the harness bridge --------------------------------------------------------


def test_an_elided_result_lands_in_tool_out_and_the_hint_says_so(
    tmp_path: Path, run: Any
) -> None:
    """Rule 1 end to end: elide, spill, inject, and a hint that is actually true."""
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line {n}" for n in range(4000)))

    harness = Harness(HarnessContext(cwd=tmp_path, repl=PyREPL(cwd=tmp_path)), approval_mode="full-auto")
    try:
        result = run(harness.invoke("read", {"path": "big.txt"}))
        assert result.output_id and tool_out_hint(result.output_id) in result.content

        cell = run(
            harness.invoke("repl", {"code": f"print(len(tool_out[{result.output_id!r}]))"})
        )
        assert cell.content.strip().isdigit() and int(cell.content.strip()) > 8192
    finally:
        run(harness.aclose())


def test_without_a_repl_the_hint_falls_back(tmp_path: Path, run: Any) -> None:
    """Rule 1, as a test: never name a place the model cannot
    reach."""
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line {n}" for n in range(4000)))

    harness = Harness(HarnessContext(cwd=tmp_path), approval_mode="full-auto")
    result = run(harness.invoke("read", {"path": "big.txt"}))
    assert "tool_out" not in result.content
    assert "narrower offset/limit" in result.content


def test_a_cell_that_prints_a_megabyte_is_capped_not_fatal(repl: PyREPL, run: Any) -> None:
    """The failure this guards is not truncation, it is a wedged pipe.

    A line past the stream limit used to raise `ValueError` out of `execute()` — past
    every handler, since a caller that asked to run a cell is not expecting one — and
    then leave the stdout transport paused, so `aclose()` never returned either.
    """
    result = run(repl.execute('print("=" * 200_000)', timeout=30))

    assert result.ok and result.outcome == "ok"
    assert len(result.stdout) < 200_000 and "dropped by the REPL" in result.stdout
    # And the namespace is still there: this was a cap, not a crash.
    assert run(repl.execute("x = 1\nprint(x)")).stdout.strip() == "1"


def test_an_over_long_result_reaches_the_model_as_a_tool_result(tmp_path: Path, run: Any) -> None:
    """`invoke` never raises for a tool-level failure — including this one."""
    harness = Harness(HarnessContext(cwd=tmp_path, repl=PyREPL(cwd=tmp_path)), approval_mode="full-auto")
    try:
        result = run(harness.invoke("repl", {"code": 'print("=" * 200_000)'}))
        assert not result.is_error and len(result.content) <= 8192
    finally:
        run(harness.aclose())
