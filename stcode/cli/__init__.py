"""
CLI — what the user sees and types: the typer entrypoint, the textual screens, and the
copy they display.

Nothing here is imported by `stcode.core`; the dependency runs one way, interface →
engine. No re-exports: every screen imports `stcode.cli.labels`, and pulling typer in
through this `__init__` to get there would be dead weight.
"""
