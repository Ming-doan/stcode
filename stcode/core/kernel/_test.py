"""
Kernel tests — against a real kernel process, no mocks.

A mocked jupyter_client would test our understanding of the protocol rather than the
protocol, and every bug worth catching here (a dropped message, an interrupt that does
not land, a namespace that quietly resets) lives in the parts a mock replaces.

One kernel is shared across the module and one event loop with it: the client's ZMQ
sockets bind to the loop that started the channels, so `asyncio.run()` per test would
leave the kernel talking to a closed loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Iterator, TypeVar

import pytest

from stcode.core.kernel import ExecOutcome, PythonKernel, elide
from stcode.core.kernel.types import ExecResult, KernelError

T = TypeVar("T")


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture(scope="module")
def kernel(loop: asyncio.AbstractEventLoop) -> Iterator[PythonKernel]:
    repl = PythonKernel()
    loop.run_until_complete(repl.start())
    try:
        yield repl
    finally:
        loop.run_until_complete(repl.shutdown())


@pytest.fixture
def run(loop: asyncio.AbstractEventLoop) -> Any:
    def _run(coro: Coroutine[Any, Any, T]) -> T:
        return loop.run_until_complete(coro)

    return _run


# ---- execution basics ----------------------------------------------------------


def test_captures_stdout_and_final_expression(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("print('hello')\n40 + 2"))
    assert result.outcome is ExecOutcome.OK
    assert result.stdout == "hello\n"
    assert result.result_repr == "42"
    assert result.execution_count is not None


def test_namespace_persists_across_cells(kernel: PythonKernel, run: Any) -> None:
    run(kernel.execute("carried = 'still here'"))
    result = run(kernel.execute("print(carried)"))
    assert result.stdout.strip() == "still here"


def test_top_level_await(kernel: PythonKernel, run: Any) -> None:
    """`await agent(...)` is only possible because ipykernel autoawaits."""
    result = run(kernel.execute("import asyncio\nawait asyncio.sleep(0.01)\n'awaited'"))
    assert result.outcome is ExecOutcome.OK
    assert result.result_repr == "'awaited'"


def test_error_is_reported_without_killing_the_session(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("1 / 0"))
    assert result.outcome is ExecOutcome.ERROR
    assert result.error is not None
    assert result.error.ename == "ZeroDivisionError"
    assert "\x1b[" not in result.error.traceback  # ANSI stripped
    assert run(kernel.execute("print(carried)")).stdout.strip() == "still here"


def test_stderr_is_separated_from_stdout(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("import sys\nprint('out')\nprint('err', file=sys.stderr)"))
    assert result.stdout.strip() == "out"
    assert "err" in result.stderr


def test_stdin_is_refused_rather_than_hanging(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("input('prompt: ')", timeout=15))
    assert result.outcome is ExecOutcome.ERROR
    assert result.error is not None and "Stdin" in result.error.ename


def test_display_data_is_collected(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("from IPython.display import display\ndisplay('shown')\nNone"))
    assert any("shown" in d for d in result.displays)


def test_on_stream_fires_as_output_arrives(kernel: PythonKernel, run: Any) -> None:
    seen: list[tuple[str, str]] = []
    run(kernel.execute("print('a')\nprint('b')", on_stream=lambda n, t: seen.append((n, t))))
    assert "".join(t for _, t in seen) == "a\nb\n"
    assert {n for n, _ in seen} == {"stdout"}


def test_results_are_not_polluted_by_the_previous_cell(kernel: PythonKernel, run: Any) -> None:
    """The pump routes by parent msg_id, so a noisy cell cannot bleed into the next."""
    run(kernel.execute("print('noise ' * 500)"))
    result = run(kernel.execute("print('clean')"))
    assert result.stdout == "clean\n"


# ---- timeout and interrupt -----------------------------------------------------


def test_timeout_interrupts_and_leaves_the_kernel_usable(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("import time\nmarker = 'set'\ntime.sleep(30)", timeout=1.5))
    assert result.outcome is ExecOutcome.TIMEOUT
    assert run(kernel.is_alive())
    # State the cell built before it was cut is still there — that is the point of
    # interrupting rather than restarting.
    assert run(kernel.execute("print(marker)")).stdout.strip() == "set"


def test_explicit_interrupt_is_distinct_from_timeout(kernel: PythonKernel, run: Any) -> None:
    async def interrupt_after_delay() -> ExecResult:
        task = asyncio.ensure_future(kernel.execute("import time; time.sleep(30)", timeout=30))
        await asyncio.sleep(1.0)
        await kernel.interrupt()
        return await task

    result = run(interrupt_after_delay())
    assert result.outcome is ExecOutcome.INTERRUPTED


# ---- bootstrap contract --------------------------------------------------------


def test_namespace_contract_is_present(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("print(sorted(k for k in ('tool_out','session_ctx','answer') if k in globals()))"))
    assert result.stdout.strip() == "['answer', 'session_ctx', 'tool_out']"
    assert run(kernel.execute("answer['ready']")).result_repr == "False"


def test_unavailable_primitives_explain_themselves(kernel: PythonKernel, run: Any) -> None:
    result = run(kernel.execute("await agent('do a thing')"))
    assert result.error is not None
    assert result.error.ename == "NotImplementedError"
    assert "phase 2" in result.error.evalue


def test_stdout_mirror_holds_what_truncation_cuts(kernel: PythonKernel, run: Any) -> None:
    """The elision marker points at `_stdout`; it has to actually be there."""
    result = run(kernel.execute("print('x' * 50_000)"))
    assert len(result.view()) <= 8192
    assert "_stdout" in result.view()
    recovered = run(kernel.execute("print(len(_stdout))"))
    # A fresh cell resets the mirror, so the length seen is of *that* cell's output.
    assert recovered.outcome is ExecOutcome.OK
    joint = run(kernel.execute("print('y' * 100)\nprint(len(_stdout))"))
    assert joint.stdout.strip().endswith("101")


# ---- truncation ----------------------------------------------------------------


def test_elide_keeps_head_and_tail() -> None:
    text = "START" + "-" * 10_000 + "END"
    out = elide(text, 200, hint="_stdout")
    assert len(out) <= 200
    assert out.startswith("START")
    assert out.endswith("END")
    assert "_stdout" in out


def test_elide_is_a_noop_under_the_limit() -> None:
    assert elide("short", 8192) == "short"


def test_view_keeps_the_traceback_when_stdout_is_huge() -> None:
    result = ExecResult(
        outcome=ExecOutcome.ERROR,
        stdout="n" * 200_000,
        error=KernelError(ename="ValueError", evalue="boom", traceback="Traceback\nValueError: boom"),
    )
    view = result.view()
    assert len(view) <= 8192
    assert "ValueError: boom" in view


# ---- restart -------------------------------------------------------------------


def test_restart_clears_state_and_rebuilds_the_contract(run: Any) -> None:
    """Restart is destructive by definition; what must survive is the bootstrap."""
    repl = PythonKernel()
    run(repl.start())
    try:
        run(repl.execute("gone_after_restart = 1"))
        run(repl.restart())
        assert run(repl.is_alive())
        assert run(repl.execute("gone_after_restart")).error is not None
        assert run(repl.execute("answer['ready']")).result_repr == "False"
    finally:
        run(repl.shutdown())
