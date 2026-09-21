"""
CLI entrypoint — one command, three shapes.

    stcode                 chat UI + a daemon (started here if none is listening)
    stcode --headless      the daemon alone — a container's whole process
    stcode --daemonless    the chat UI alone, pointed at a daemon somewhere else

One binary because they are one system, and nothing switches implementation:
`--headless` skips the UI, `--daemonless` skips starting a daemon, and the socket
between them is the same socket.

Transport and address flags override `[daemon]` for this run only.

`stcode <path>` opens a session in that directory. It is a rewrite to `--cwd` rather
than an argument on the group, because a `click` group consumes its own arguments before
it looks for a subcommand — declaring one there makes `stcode sessions` mean "open the
directory ./sessions". Only the **first** token is read this way, and only when it is
not a subcommand, so the grammar stays "the path comes first, flags after".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any, Sequence

import typer

from stcode import __version__
from stcode.core.configs import (
    GatewayConfig,
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
        typer.Option(
            "--cwd",
            "-C",
            help="Workspace for the session. Defaults to the current directory. "
            "`stcode <path>` is the same thing, spelled shorter.",
        ),
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
            help="Which agent this is, e.g. backend-dev. Selects the profile, and "
            "therefore the prompt. Does not by itself turn team mode on.",
        ),
    ] = None,
    team: Annotated[
        bool | None,
        typer.Option(
            "--team/--no-team",
            envvar="STCODE_TEAM",
            help="Turn team mode on: the shared volume, the inbox and send_message. "
            "Off unless this, STCODE_TEAM, or [team] enabled says otherwise.",
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
        "team": team,
    }

    if headless:
        _serve(config, overrides)
        return

    # Resolved, not kept as typed: `.` reaches the prefs file as the trusted path, the
    # daemon as the session's cwd and the UI as the root it lists files under, and a
    # relative one means something different in each of them.
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    if cwd is not None and not cwd.is_dir():
        # Said here rather than three screens later: the workspace is the one argument
        # everything else is relative to, and a typo in it looks like an agent that
        # cannot see your files.
        typer.secho(f"{cwd} is not a directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

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


def startup_report(
    settings: "GatewayConfig",
    *,
    address: str,
    max_clients: int,
    config_path: Path,
    created: Sequence[Path] = (),
    agents: Sequence[str] = (),
    contained: bool = False,
) -> list[str]:
    """The block `--headless` prints, one line per fact.

    A container's daemon has no screen, so start-up is the only place it can say what it
    became — and every line here answers a question you would otherwise `docker exec` to
    ask. Two earn their place beyond that:

    * **`created`** names what this start-up *made*. A config scaffolded because a mount
      silently did not happen looks exactly like a config that was mounted, and it is
      the most expensive thing on this list to discover late.
    * **`container`** is what decides whether `full-auto` may start at all (rule 5), so
      it is reported rather than assumed.

    A list of strings rather than prints, because the interesting part is *what* it
    says, and a function that writes to stdout can only be tested by capturing it.
    """
    defaults = settings.defaults
    lines = [
        f"stcode {__version__} — headless daemon",
        f"  listening   {settings.daemon.transport} {address}"
        f"   ({max_clients} client{'' if max_clients == 1 else 's'} max)"
        if max_clients
        else f"  listening   {settings.daemon.transport} {address}   (no client limit)",
        f"  workspace   {Path.cwd()}",
        f"  mode        {defaults.approval_mode}",
        f"  model       {defaults.model or '(not set)'} ({defaults.provider})",
        f"  config      {config_path}",
        f"  sessions    {Path(settings.session.dir).expanduser()}",
    ]
    if agents:
        lines.append(f"  agents      {', '.join(agents)}")
    if settings.team.enabled:
        lines.append(f"  team        {settings.team.role or '(no role!)'} on {settings.team.shared_dir}")
    else:
        lines.append("  team        off")
    lines.append(f"  container   {'yes' if contained else 'no'}")
    for path in created:
        lines.append(f"  created     {path}")
    return lines


def _serve(config: Path | None, overrides: dict[str, Any]) -> None:
    """`--headless`: the daemon and nothing else."""
    import asyncio
    import logging

    from stcode.core.daemon import AutonomyRefused, Daemon
    from stcode.core.daemon.autonomy import in_container
    from stcode.core.harness.prompts import available_agents

    # INFO in this shape and nowhere else. A UI has a transcript to say what happened;
    # a daemon has stdout, and `docker logs` is how anybody reads it. Without this every
    # `log.info` the daemon already writes — sessions created, clients connecting — went
    # to a logger with no handler.
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    path = config or default_config_path()
    existed = config_exists(path)
    settings = apply_cli_overrides(load_config(path, create_if_missing=True), **overrides)

    created: list[Path] = [] if existed else [path]
    sessions = Path(settings.session.dir).expanduser()
    if not sessions.exists():
        created.append(sessions)

    async def serve() -> None:
        daemon = Daemon(settings, config_path=path)
        await daemon.start()
        for line in startup_report(
            settings,
            address=daemon.address,
            max_clients=daemon.max_clients,
            config_path=path,
            created=created,
            agents=available_agents(),
            contained=in_container(),
        ):
            typer.echo(line)
        typer.echo("ready — ctrl-c to stop")
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
        Path(first).expanduser().is_dir() or first.startswith(("~", ".", "/")) or "/" in first
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
