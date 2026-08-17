"""
CLI entrypoint — `stcode` launches the TUI; everything else is a subcommand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from stcode import __version__
from stcode.core.configs import config_exists, default_config_path, load_config

cli = typer.Typer(
    name="stcode",
    help="stcode — a recursive-language-model coding agent for the terminal.",
    no_args_is_help=False,
    add_completion=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Config file to use instead of the default location."),
]


@cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: ConfigOption = None,
    version: Annotated[
        bool, typer.Option("--version", help="Print the version and exit.")
    ] = False,
) -> None:
    """Start the chat UI when no subcommand is given."""
    if version:
        typer.echo(f"stcode {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Imported lazily: `stcode config` shouldn't pay for loading textual.
        from stcode.cli.app import run

        run(config)


@cli.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Show where the config lives and what it currently selects."""
    path = config or default_config_path()
    if not config_exists(path):
        typer.echo(f"No config at {path} — run `stcode` and the setup screen will offer to make one.")
        raise typer.Exit(code=1)

    defaults = load_config(path).defaults
    typer.echo(f"path:     {path}")
    typer.echo(f"provider: {defaults.provider}")
    typer.echo(f"model:    {defaults.model or '(not set — run /model in the UI)'}")
    typer.echo(f"mode:     {defaults.approval_mode}")


if __name__ == "__main__":
    cli()
