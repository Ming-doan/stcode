"""
CLI entrypoint — one command, three shapes.

    stcode                 chat UI + a daemon (started here if none is listening)
    stcode --headless      the daemon alone — a container's whole process
    stcode --daemonless    the chat UI alone, pointed at a daemon somewhere else

The daemon is its own program, `stcode-daemon`. `--headless` replaces this process with
it; the UI starts it in the background when nothing listens, and stops it on exit.
`--restart` stops a running local daemon first, for when it is wedged or its code
changed.

`stcode <path>` opens a session in that directory. It is a rewrite to `--cwd` rather
than an argument on the group, because a `click` group consumes its own arguments before
it looks for a subcommand — declaring one there makes `stcode sessions` mean "open the
directory ./sessions". Only the **first** token is read this way, and only when it is
not a subcommand, so the grammar stays "the path comes first, flags after".
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, NoReturn

import typer

from stcode import __version__
from stcode.cli.services.client import Address, DaemonClient
from stcode.cli.models import APPROVAL_MODES, ApprovalMode
from stcode.cli.logic.commands import parse_approval_mode
from stcode.cli.services.launcher import DaemonFailed, Launcher, daemon_command

cli = typer.Typer(
    name="stcode",
    help="stcode — a coding agent for the terminal: a daemon holding the agent, and a UI attached to it.",
    no_args_is_help=False,
    add_completion=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="Config file to use instead of the default location."
    ),
]


@cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: ConfigOption = None,
    headless: Annotated[
        bool,
        typer.Option(
            "--headless", help="Run the daemon only, with no UI. What a container runs."
        ),
    ] = False,
    daemonless: Annotated[
        bool,
        typer.Option(
            "--daemonless",
            help="Run the UI only, attached to a daemon elsewhere. Asks for the address if none answers.",
        ),
    ] = False,
    restart: Annotated[
        bool,
        typer.Option(
            "--restart", help="Stop the local daemon first and start a fresh one."
        ),
    ] = False,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            "-C",
            help="Workspace for the session. Defaults to the current directory. "
            "`stcode <path>` is the same thing, spelled shorter.",
        ),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode", "-m", help=f"Approval mode: {' | '.join(APPROVAL_MODES)}."
        ),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Attach to an existing session id instead of starting one.",
        ),
    ] = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="Which agent this is, e.g. backend-dev."),
    ] = None,
    team: Annotated[
        bool,
        typer.Option("--team", help="Headless only: turn team mode on."),
    ] = False,
    transport: Annotated[
        str | None, typer.Option("--transport", help="unix | tcp.")
    ] = None,
    socket: Annotated[
        str | None, typer.Option("--socket", help="Unix socket path.")
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="TCP host to bind (headless) or connect to."),
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="TCP port. Default 7717.")
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", help="Print the version and exit.")
    ] = False,
) -> None:
    """Start stcode. With no subcommand this is the whole program."""
    if version:
        typer.echo(f"stcode {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        ctx.obj = _address(transport, socket, host, port), config
        return
    if headless and daemonless:
        _fail(
            "--headless and --daemonless are opposites: one is the daemon without a UI, "
            "the other the UI without a daemon."
        )
    approval_mode = _mode(mode)

    if headless:
        # Flags pass through by name: `stcode-daemon` is the one that understands them.
        flags = {
            "--config": config,
            "--transport": transport,
            "--socket": socket,
            "--host": host,
            "--port": port,
            "--mode": approval_mode,
            "--role": role,
        }
        command = daemon_command()
        command += [
            str(item)
            for flag, value in flags.items()
            if value is not None
            for item in (flag, value)
        ]
        command += ["--team"] if team else []
        os.execv(command[0], command)

    # Resolved, not kept as typed: `.` reaches the prefs file as the trusted path, the
    # daemon as the session's cwd and the UI as the root it lists files under, and a
    # relative one means something different in each of them.
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    if cwd is not None and not cwd.is_dir():
        _fail(f"{cwd} is not a directory.")

    # Imported lazily: `stcode sessions` shouldn't pay for loading textual.
    from stcode.cli.bootstrap import run

    run(
        Launcher(_address(transport, socket, host, port), config=config),
        daemonless=daemonless,
        restart=restart,
        cwd=cwd,
        resume=resume or "",
        mode=approval_mode,
        role=role or "",
    )


def _address(
    transport: str | None, socket: str | None, host: str | None, port: int | None
) -> Address:
    address = Address(transport=transport or "unix")
    if socket:
        address.socket = Path(socket).expanduser()
    address.host = host or address.host
    address.port = port or address.port
    return address


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def _mode(value: str | None) -> ApprovalMode | None:
    """Resolve `--mode auto` to a mode name, or exit saying what the choices are."""
    if value is None:
        return None
    resolved = parse_approval_mode(value)
    if resolved is None:
        _fail(f"Unknown mode {value!r}. One of: {', '.join(APPROVAL_MODES)}")
    return resolved


def _with_daemon(
    ctx: typer.Context, action: Callable[[DaemonClient], Awaitable[None]]
) -> None:
    """Run one request against the daemon, starting it for the duration if needed."""
    address, config = ctx.obj

    async def go() -> None:
        launcher = Launcher(address, config=config)
        try:
            client = await launcher.connect()
        except (OSError, DaemonFailed) as exc:
            _fail(f"Could not reach a daemon at {address}: {exc}")
        try:
            async with client:
                await action(client)
        finally:
            await launcher.stop()

    asyncio.run(go())


@cli.command("sessions")
def list_sessions(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 20,
) -> None:
    """List recent sessions, newest first. Resume one with `stcode --resume <id>`."""

    async def show(client: DaemonClient) -> None:
        rows = await client.sessions(limit)
        if not rows:
            typer.echo("No sessions yet.")
        for row in rows:
            typer.echo(
                f"{row.get('id', '?'):<28} {row.get('ts', ''):<26} {row.get('cwd', '')}"
            )

    _with_daemon(ctx, show)


@cli.command("config")
def show_config(ctx: typer.Context) -> None:
    """Show the daemon's config file and what it currently selects."""

    async def show(client: DaemonClient) -> None:
        frame: dict[str, Any] = await client.get_config()
        typer.echo(f"path:     {frame.get('path')}")
        typer.echo(f"provider: {frame.get('provider')}")
        typer.echo(f"model:    {frame.get('model')}")
        typer.echo(f"effort:   {frame.get('reasoning_effort')}")
        typer.echo(f"mode:     {frame.get('approval_mode')}")
        typer.echo(f"daemon:   {ctx.obj[0]}")

    _with_daemon(ctx, show)


def workspace_argv(argv: list[str], commands: set[str]) -> list[str]:
    """Rewrite a leading path into `--cwd <path>`. Everything else is untouched.

    The first token only, and only when it is neither a flag nor a subcommand. A token
    that is neither but also does not look like a path — `stcode sesions` — is left
    alone, so a mistyped command still gets click's "No such command" instead of being
    silently accepted as a directory that does not exist.
    """
    if not argv:
        return argv
    first = argv[0]
    if first.startswith("-") or first in commands:
        return argv
    looks_like_a_path = (
        Path(first).expanduser().is_dir()
        or first.startswith(("~", ".", "/"))
        or "/" in first
    )
    if not looks_like_a_path:
        return argv
    return ["--cwd", first, *argv[1:]]


def run_cli() -> None:
    """The console entry point. → `workspace_argv`."""
    commands = set(typer.main.get_command(cli).commands)  # type: ignore[attr-defined]
    cli(args=workspace_argv(sys.argv[1:], commands))


if __name__ == "__main__":
    run_cli()
