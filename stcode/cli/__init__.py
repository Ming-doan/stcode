"""
CLI — what the user sees and types: the typer entrypoint, the textual screens, and the
copy they display.

A separate program from `stcode.core`: neither imports the other. The UI reaches the
engine only through the daemon's socket (`client.py`), and starts one as a subprocess
when none is listening (`launcher.py`). No re-exports: every screen imports
`stcode.cli.labels`, and pulling typer in through this `__init__` would be dead weight.
"""
