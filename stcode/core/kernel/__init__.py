"""
Kernel — the persistent Python REPL that is the main agent's only tool.

    async with PythonKernel(cwd=project_root) as repl:
        result = await repl.execute("print(len(tool_out))")
        print(result.view())          # what the agent sees, capped at 8192 chars
        print(result.stdout)          # what actually happened, in full
"""

from stcode.core.kernel.bootstrap import BOOTSTRAP_CODE
from stcode.core.kernel.kernel import (
    DEFAULT_TIMEOUT,
    KernelStartupError,
    PythonKernel,
    StreamCallback,
)
from stcode.core.kernel.truncate import DEFAULT_VIEW_LIMIT, elide
from stcode.core.kernel.types import ExecOutcome, ExecResult, KernelError

__all__ = [
    "BOOTSTRAP_CODE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_VIEW_LIMIT",
    "ExecOutcome",
    "ExecResult",
    "KernelError",
    "KernelStartupError",
    "PythonKernel",
    "StreamCallback",
    "elide",
]
