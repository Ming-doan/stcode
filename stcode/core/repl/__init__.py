"""
`core/repl/` — a persistent Python namespace, in a subprocess.

Two files: `_worker.py` is the child, `client.py` is the parent. They speak JSONL over
stdin/stdout — the same framing as the session file and the daemon protocol.

It exists for MCP-as-code: somewhere to `import` a generated stub and hold the result
across turns, so the bulk never touches the context window.
"""

from stcode.core.repl.client import (
    DEFAULT_TIMEOUT,
    ExecResult,
    PyREPL,
    ReplError,
    StreamFn,
)

__all__ = ["DEFAULT_TIMEOUT", "ExecResult", "PyREPL", "ReplError", "StreamFn"]
