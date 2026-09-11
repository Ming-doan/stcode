"""
CLI entrypoint — one command, three shapes.

    stcode                 chat UI + a daemon (started here if none is listening)
    stcode --headless      the daemon alone — a container's whole process
    stcode --daemonless    the chat UI alone, pointed at a daemon somewhere else

One binary because they are one system, and nothing switches implementation:
`--headless` skips the UI, `--daemonless` skips starting a daemon, and the socket
between them is the same socket.

Transport and address flags override `[daemon]` for this run only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from stcode import __version__
from stcode.core.configs import (
    apply_cli_overrides,
    config_exists,
    default_config_path,
    load_config,
)
from stcode.core.harness.approvals import APPROVAL_MODES, ApprovalMode, parse_approval_mode

cli = typer.Typer(
    name="stcode",
    help="stcode — a coding agent for the terminal: a daemon holding the agent, and a UI attached to it.",
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
    headless: Annotated[
        bool,
        typer.Option("--headless", help="Run the daemon only, with no UI. What a container runs."),
    ] = False,
    daemonless: Annotated[
        bool,
        typer.Option(
            "--daemonless",
            help="Run the UI only, attached to a daemon elsewhere. Asks for the address if none answers.",
        ),
    ] = False,
    cwd: Annotated[
        Path | None,
        typer.Option("--cwd", "-C", help="Workspace for the session. Defaults to the current directory."),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option("--mode", "-m", help=f"Approval mode: {' | '.join(APPROVAL_MODES)}."),
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Model for this run, overriding [defaults] model.")
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", "-r", help="Attach to an existing session id instead of starting one."),
    ] = None,
    role: Annotated[
        str | None,
        typer.Option(
            "--role",
            envvar="STCODE_ROLE",
            help="Team role for this run, e.g. backend-dev. Turns on team mode: the "
            "shared volume, the role prompt, and send_message.",
        ),
    ] = None,
    transport: Annotated[
        str | None, typer.Option("--transport", help="unix | tcp. Overrides [daemon] transport.")
    ] = None,
    socket: Annotated[
        str | None, typer.Option("--socket", help="Unix socket path to listen on or connect to.")
    ] = None,
    host: Annotated[
        str | None, typer.Option("--host", help="TCP host to bind (headless) or connect to.")
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="TCP port. Default 7717.")] = None,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    """Start stcode. With no subcommand this is the whole program."""
    if version:
        typer.echo(f"stcode {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    if headless and daemonless:
        # Neither half left. Saying so beats starting something that does nothing.
        typer.secho(
            "--headless and --daemonless are opposites: one is the daemon without a UI, "
            "the other the UI without a daemon.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    overrides: dict[str, Any] = {
        "transport": transport,
        "socket": socket,
        "host": host,
        "port": port,
        "approval_mode": _mode(mode),
        "model": model,
        "role": role,
    }

    if headless:
        _serve(config, overrides)
        return

    # Imported lazily: `stcode config` and `--headless` shouldn't pay for loading textual.
    from stcode.cli.app import run

    run(config, daemonless=daemonless, cwd=cwd, resume=resume or "", overrides=overrides)


def _mode(value: str | None) -> ApprovalMode | None:
    """Resolve `--mode auto` to a mode name, or exit saying what the choices are."""
    if value is None:
        return None
    resolved = parse_approval_mode(value)
    if resolved is None:
        typer.secho(
            f"Unknown mode {value!r}. One of: {', '.join(APPROVAL_MODES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return resolved


def _serve(config: Path | None, overrides: dict[str, Any]) -> None:
    """`--headless`: the daemon and nothing else."""
    import asyncio

    from stcode.core.daemon import AutonomyRefused, Daemon

    settings = apply_cli_overrides(load_config(config, create_if_missing=True), **overrides)

    async def serve() -> None:
        daemon = Daemon(settings)
        await daemon.start()
        typer.echo(f"stcode daemon listening on {daemon.address}  (ctrl-c to stop)")
        try:
            await daemon.serve_forever()
        finally:
            await daemon.aclose()

    try:
        asyncio.run(serve())
    except AutonomyRefused as refusal:
        # Invariant 5. The engine refuses; this only turns the refusal into an exit code.
        # There is no flag here that could have prevented it, and there will not be one.
        typer.secho(str(refusal), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    except KeyboardInterrupt:
        typer.echo("stopped")


@cli.command("sessions")
def list_sessions(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 20,
) -> None:
    """List recent sessions, newest first. Resume one with `stcode --resume <id>`."""
    from stcode.core.session import Session

    settings = load_config(config, create_if_missing=True)
    rows = Session.list(limit, directory=settings.session.dir)
    if not rows:
        typer.echo("No sessions yet.")
        return
    for row in rows:
        typer.echo(f"{row.get('id', '?'):<28} {row.get('ts', ''):<26} {row.get('cwd', '')}")


@cli.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Show where the config lives and what it currently selects."""
    path = config or default_config_path()
    if not config_exists(path):
        typer.echo(f"No config at {path} — run `stcode` and the setup screen will offer to make one.")
        raise typer.Exit(code=1)

    settings = load_config(path)
    daemon = settings.daemon
    address = daemon.socket if daemon.transport == "unix" else f"{daemon.host}:{daemon.port}"
    typer.echo(f"path:     {path}")
    typer.echo(f"provider: {settings.defaults.provider}")
    typer.echo(f"model:    {settings.defaults.model or '(not set — run /model in the UI)'}")
    typer.echo(f"mode:     {settings.defaults.approval_mode}")
    typer.echo(f"daemon:   {daemon.transport} {address}")
    typer.echo(f"sessions: {settings.session.dir}")


if __name__ == "__main__":
    cli()
