"""
Tool failures — the vocabulary for something going wrong, shared by everything here.

A leaf module on purpose. `context.py` needs to raise these and `tools/base.py` needs
to catch them, and putting them in either one makes the other import it — which is a
cycle, since a tool imports the context it operates on. Exceptions are shared
vocabulary rather than machinery, so they live below both.

The distinctions are not decorative; the agent loop reacts differently to each:

* `ToolError` — the tool worked and the answer is "no, because…". The message goes
  straight back to the model as the tool result.
* `ToolDenied` — a human said no. Could go the other way next time.
* `ToolForbidden` — the mode does not have this capability. Retrying cannot help, and
  the model should be told to stop rather than to try again.
* `ToolCancelled` / `ToolTimeout` — the call did not finish. Different remedies:
  interruption means the user wants something else, a timeout means narrow the request.

A bug *inside* a tool is none of these. It surfaces as whatever it is, gets logged with
its traceback, and is summarized for the model — a stack frame is not advice.
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
