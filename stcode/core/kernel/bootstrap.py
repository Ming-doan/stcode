"""
Bootstrap — the namespace contract every session's kernel starts with.

This is the code the model's REPL wakes up inside, so it is kept as one readable source
string rather than assembled from fragments: what is written here is exactly what the
agent can reach, and CLAUDE.md §2.1 is its specification.

It deliberately imports nothing from `stcode`. The kernel is a separate process, and
making the REPL depend on this package being importable inside it turns an install
detail into a startup failure. Richer objects (`tool_out` with spill-to-disk, `agent`,
`harness`) get injected by their owning layers once those exist.
"""

from __future__ import annotations

BOOTSTRAP_CODE = '''
import io as _io, sys as _sys


class _Tee:
    """Mirrors a stream into a buffer on its way to the real one.

    Truncation is only non-lossy if the full text survives somewhere the agent can
    reach. Tool results live in `tool_out`; plain `print()` output would otherwise be
    gone the moment it was cut, so both standard streams are mirrored here and the
    elision marker can honestly point at `_stdout` / `_stderr`.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.buffer_text = _io.StringIO()

    def write(self, text):
        self.buffer_text.write(text)
        return self._wrapped.write(text)

    def flush(self):
        return self._wrapped.flush()

    def reset(self):
        self.buffer_text = _io.StringIO()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _Mirror:
    """String-like view of a tee'd stream. Slices and prints like the `str` it stands in
    for, but reads through to the live buffer so `_stdout[8192:]` is current."""

    def __init__(self, tee):
        self._tee = tee

    def _value(self):
        return self._tee.buffer_text.getvalue()

    def __str__(self):
        return self._value()

    def __repr__(self):
        return repr(self._value())

    def __len__(self):
        return len(self._value())

    def __getitem__(self, key):
        return self._value()[key]

    def __contains__(self, item):
        return item in self._value()

    def __iter__(self):
        return iter(self._value())

    def __getattr__(self, name):
        return getattr(self._value(), name)


def _install_tees():
    tees = {}
    for name in ("stdout", "stderr"):
        stream = getattr(_sys, name)
        if not isinstance(stream, _Tee):
            stream = _Tee(stream)
            setattr(_sys, name, stream)
        tees[name] = stream
    return tees


_tees = _install_tees()
_stdout = _Mirror(_tees["stdout"])
_stderr = _Mirror(_tees["stderr"])

try:
    _ip = get_ipython()
except NameError:
    _ip = None

if _ip is not None:
    def _reset_mirrors(*_args, **_kwargs):
        for _tee in _tees.values():
            _tee.reset()

    # Per-cell, not per-session: the mirror answers "what did *this* cell print", which
    # is what a truncated view needs to point at. A session-long buffer would grow
    # without bound and index into the wrong output.
    for _existing in list(_ip.events.callbacks.get("pre_run_cell", [])):
        if getattr(_existing, "__name__", "") == "_reset_mirrors":
            _ip.events.unregister("pre_run_cell", _existing)
    _ip.events.register("pre_run_cell", _reset_mirrors)


tool_out = {}
"""Raw tool results, keyed by call id. The agent slices these; it never prints them whole."""

session_ctx = {}
"""Knowledge shared down into sibling sub-agents."""

answer = {"content": "", "ready": False}
"""The only channel for the final response. Set `ready` to True to commit it."""


class _NotYetAvailable:
    """Placeholder for a REPL primitive a later phase will inject.

    Present rather than absent on purpose: a bare NameError tells the model it typed
    something wrong, which sends it looking for a different spelling. This tells it the
    capability does not exist yet and what to do instead.
    """

    def __init__(self, name, guidance):
        self._name = name
        self._guidance = guidance

    def _fail(self, *_args, **_kwargs):
        raise NotImplementedError(f"`{self._name}` is not available yet. {self._guidance}")

    __call__ = _fail
    __getattr__ = lambda self, _name: self._fail

    def __repr__(self):
        return f"<{self._name}: unavailable — {self._guidance}>"


agent = _NotYetAvailable("agent", "Sub-agents land in phase 2; do the work in this REPL.")
agent_message = _NotYetAvailable("agent_message", "Agent-to-agent messaging lands in phase 3.")
compact = _NotYetAvailable("compact", "Compaction lands in phase 2.")
refine = _NotYetAvailable("refine", "The continual harness lands in phase 4.")
harness = _NotYetAvailable("harness", "Harness CRUD lands in phase 4.")

del _install_tees
'''
