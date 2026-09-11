"""
Tool failures — the vocabulary for something going wrong.

A leaf module: `context.py` raises these and `tools/base.py` catches them, so putting
them in either would make a cycle. The agent loop reacts differently to each:

* `ToolError` — the tool worked, and the answer is "no, because…".
* `ToolDenied` — a human said no. Could go the other way next time.
* `ToolForbidden` — the mode lacks this capability. Retrying cannot help.
* `ToolCancelled` / `ToolTimeout` — did not finish. An interrupt means the user wants
  something else; a timeout means narrow the request.

A bug *inside* a tool is none of these: logged with its traceback, summarised for the
model. A stack frame is not advice.
"""

from __future__ import annotations


class ToolError(Exception):
    """A tool failed in a way the model should read and react to."""


class ToolDenied(ToolError):
    """The human was asked to approve this call and said no."""


class ToolForbidden(ToolError):
    """The current approval mode does not have this capability at all."""


class ToolCancelled(ToolError):
    """The session was interrupted while this tool was running."""


class ToolTimeout(ToolError):
    """The tool exceeded its time budget."""


__all__ = ["ToolCancelled", "ToolDenied", "ToolError", "ToolForbidden", "ToolTimeout"]
