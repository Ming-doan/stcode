"""
`stcode-daemon` — the daemon as a process. What a container runs, and what the UI
starts in the background when nothing is listening.

    stcode-daemon [--config PATH] [--transport unix|tcp] [--socket PATH]
                  [--host HOST] [--port N] [--mode MODE] [--role ROLE] [--team]

Flags apply to this process only and are never written to the config file. It logs to
stderr and stops on SIGTERM, SIGINT, or a `shutdown` message over the unix socket.

Exit codes: 0 stopped, 1 failed to start, 2 `full-auto` refused (rule 5).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Sequence

from stcode import __version__
from stcode.core.configs import APPROVAL_MODES, Config, config_path, load_config
from stcode.core.daemon.autonomy import AutonomyRefused, in_container
from stcode.core.daemon.server import Daemon

log = logging.getLogger("stcode.daemon")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="stcode-daemon", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, help="Config file. Default: $STCODE_CONFIG or ~/.stcode/config.toml.")
    parser.add_argument("--transport", choices=("unix", "tcp"))
    parser.add_argument("--socket", help="Unix socket path.")
    parser.add_argument("--host", help="TCP host to bind.")
    parser.add_argument("--port", type=int, help="TCP port to bind.")
    parser.add_argument("--mode", choices=APPROVAL_MODES, help="Default approval mode for new sessions.")
    parser.add_argument("--role", help="Which agent this is. Does not turn team mode on.")
    parser.add_argument("--team", action="store_true", default=None, help="Turn team mode on.")
    parser.add_argument("--version", action="version", version=f"stcode {__version__}")
    return parser.parse_args(argv)


def overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    """The flags as dotted config keys, unset ones left out."""
    pairs = {
        "daemon.transport": args.transport,
        "daemon.socket": args.socket,
        "daemon.host": args.host,
        "daemon.port": args.port,
        "agent.approval_mode": args.mode,
        "team.role": args.role,
        "team.enabled": args.team,
    }
    return {key: value for key, value in pairs.items() if value is not None}


def startup_report(daemon: Daemon, *, created: Sequence[Path] = ()) -> list[str]:
    """What this daemon became, one line per fact — a container has no screen, so this
    is the only place to say it. `created` names what start-up *made*: a config
    scaffolded because a mount did not happen looks exactly like one that was mounted.
    """
    config = daemon.config
    limit = daemon.max_clients
    provider, model = daemon.gateway().resolve(config.agent.difficulty)
    lines = [
        f"stcode {__version__} — daemon",
        f"  listening   {config.daemon.transport} {daemon.address}   "
        + (f"({limit} client{'' if limit == 1 else 's'} max)" if limit else "(no client limit)"),
        f"  workspace   {config.cwd}",
        f"  mode        {config.agent.approval_mode}",
        f"  model       {model or '(library default)'} ({provider})",
        f"  config      {config.path}",
        f"  sessions    {config.session.dir}",
    ]
    team = config.team
    lines.append(f"  team        {team.role or '(no role!)'} on {team.shared_dir}" if team.enabled else "  team        off")
    lines.append(f"  container   {'yes' if in_container() else 'no'}")
    lines += [f"  created     {path}" for path in created]
    return lines


async def serve(config: Config, overrides: dict[str, Any], created: Sequence[Path]) -> None:
    daemon = Daemon(config, overrides=overrides)
    await daemon.start()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers
            loop.add_signal_handler(signum, daemon.stop)
    for line in startup_report(daemon, created=created):
        print(line, file=sys.stderr)
    print("ready", file=sys.stderr, flush=True)
    try:
        await daemon.serve_forever()
    finally:
        await daemon.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    overrides = overrides_from(args)
    path = (args.config or config_path()).expanduser()
    created = [] if path.exists() else [path]
    try:
        config = load_config(path, overrides=overrides, create_if_missing=True)
        if not config.session.dir.expanduser().exists():
            created.append(config.session.dir)
        asyncio.run(serve(config, overrides, created))
    except AutonomyRefused as refusal:
        # Rule 5. The engine refuses; this only turns the refusal into an exit code.
        print(str(refusal), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 — a daemon that cannot start says why and exits
        print(f"stcode-daemon: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
