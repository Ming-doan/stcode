"""
CLI — everything the user sees and types: the typer entrypoint, the textual screens,
the components they're built from, and the copy they display.

Nothing in here is imported by `stcode.core`; the dependency runs one way, interface →
engine. Deliberately no re-exports: `stcode.cli.labels` is imported by every screen, and
pulling typer in through the package `__init__` to get there would be dead weight.
"""
