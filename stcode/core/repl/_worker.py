"""
The REPL subprocess. Run as `python -u _worker.py`.

One line in, one or more lines out.

    in   {"type":"exec","id":"c1","code":"x = 1\nprint(x)"}
    out  {"type":"chunk","text":"1"}            <- while the cell runs
    out  {"type":"result","id":"c1","ok":true,"stdout":"1\n","error":null}

    in   {"type":"inject","id":"i1","values":{"read_ab12": "..."}}
    out  {"type":"ok","id":"i1"}

`ns` persists between cells. `tool_out` lives in `ns` and `inject` fills it, so the
elided half of a tool result really is reachable. Top-level `await` works, via
`PyCF_ALLOW_TOP_LEVEL_AWAIT`.

Dependency-free and imported by nothing: it is spawned, not imported.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import sys
import traceback
from typing import Any

# The real stdout, grabbed before any redirect. Cell output is redirected away from
# sys.stdout; this handle is the protocol and must never be.
_PROTO = sys.stdout
_REAL_STDERR = sys.stderr

ns: dict[str, Any] = {"tool_out": {}}

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def emit(message: dict[str, Any]) -> None:
    """One JSON object, one line, flushed."""
    _PROTO.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    _PROTO.flush()


class _Tee(io.TextIOBase):
    """Collects the cell's output, and streams it out a line at a time.

    Two readers want the same bytes for different reasons: the caller wants the whole
    thing at the end, the UI wants it now so a 90-second cell does not look frozen.
    Streaming per line rather than per `write()` because `print` makes several writes
    per line and one frame each would be noise.
    """

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self._pending = ""

    def write(self, text: str) -> int:
        self.buffer.write(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            emit({"type": "chunk", "text": line})
        return len(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        """The real stderr's fd.

        Needed because `subprocess.Popen` asks for one, and a cell that spawns a
        process is not exotic — launching a stdio MCP server is the main thing this
        REPL exists for. Without this, `redirect_stderr` turns every such call into
        `io.UnsupportedOperation: fileno`.

        A child's output therefore goes to the worker's own stderr rather than into
        this buffer. The parent drains that continuously, so a chatty server cannot
        fill the pipe and deadlock.
        """
        return _REAL_STDERR.fileno()

    def drain(self) -> None:
        """Emit the last line when it had no trailing newline."""
        if self._pending:
            emit({"type": "chunk", "text": self._pending})
            self._pending = ""


def run_cell(code: str) -> tuple[bool, str, str | None]:
    """Execute one cell. Returns (ok, stdout, traceback)."""
    tee = _Tee()
    ok, error = True, None
    try:
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            compiled = compile(code, "<cell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            # `eval` of exec-mode code returns None, unless the cell had a top-level
            # await — then it is a coroutine and we drive it here.
            pending = eval(compiled, ns)  # noqa: S307 — running the model's code is the job
            if pending is not None:
                _loop.run_until_complete(pending)
    except BaseException:  # noqa: BLE001 — KeyboardInterrupt is an outcome, not a crash
        ok, error = False, traceback.format_exc()
    tee.drain()
    return ok, tee.buffer.getvalue(), error


def main() -> None:
    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            # A SIGINT that landed after the cell had already returned. Nothing to
            # interrupt; going back to waiting is the correct response.
            continue
        if not line:
            return  # stdin closed — the parent is gone
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue

        kind = request.get("type", "exec")
        request_id = str(request.get("id", ""))

        if kind == "inject":
            ns["tool_out"].update(request.get("values") or {})
            emit({"type": "ok", "id": request_id})
            continue
        if kind == "ping":
            emit({"type": "ok", "id": request_id})
            continue

        ok, stdout, error = run_cell(str(request.get("code", "")))
        emit({"type": "result", "id": request_id, "ok": ok, "stdout": stdout, "error": error})


if __name__ == "__main__":
    main()
