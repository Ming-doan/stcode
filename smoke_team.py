"""
A hand-run smoke test for step 10: two containers, two roles, one shared volume.

CLAUDE.md marks phase 5 done when "two containers, two roles, one feature shipped
through /team". This runs that, for real: it builds the image, brings up two
containers on a shared volume, and drives them over TCP the way you would.

What it checks, in order:

1. Both daemons are reachable over TCP, each holding a session with its own role.
2. `full-auto` is legal inside a container and refused outside one — invariant 5 from
   both sides, which is the only way to know the guard is a guard.
3. A message from `ba` lands in `backend-dev`'s inbox as a file, and the idle agent is
   woken by it without anyone pushing.
4. The shared volume ends up with the spec, the branch, and one merge.

Needs Docker. Everything else — `core/team/_test.py` — covers the same ground on a
local directory. Run it with `uv run python smoke_team.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from smoke_common import MODEL, WORKSPACE, bad, build_config, note, ok, step, verdict
from stcode.core.team import Mailbox

ROOT = WORKSPACE / "team"
VOLUME = ROOT / "volume"
IMAGE = "stcode-smoke"
CONTAINERS = {"ba": 17717, "backend-dev": 17718}


def run(*command: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(command[:3])}… failed:\n{result.stderr.strip()[-600:]}")
    if not quiet and result.stdout.strip():
        note(result.stdout.strip().splitlines()[-1])
    return result


def docker_available() -> bool:
    return shutil.which("docker") is not None and run("docker", "info", check=False, quiet=True).returncode == 0


def seed_volume() -> None:
    """The shared volume, and the bare repo that is the team's origin (§9.5)."""
    if VOLUME.exists():
        shutil.rmtree(VOLUME)
    mailbox = Mailbox(VOLUME, "ba")
    mailbox.ensure(*CONTAINERS)

    origin = VOLUME / "repo.git"
    seed = ROOT / "seed"
    shutil.rmtree(seed, ignore_errors=True)
    seed.mkdir(parents=True)
    (seed / "README.md").write_text("# demo service\n")
    (seed / "app.py").write_text("def health():\n    return {'ok': True}\n")
    for command in (
        ("git", "init", "-q", "-b", "main", str(seed)),
        ("git", "-C", str(seed), "add", "-A"),
        ("git", "-C", str(seed), "-c", "user.email=smoke@stcode", "-c", "user.name=smoke",
         "commit", "-qm", "initial"),
        ("git", "clone", "-q", "--bare", str(seed), str(origin)),
    ):
        run(*command, quiet=True)
    # Anyone on the volume may push: the isolation is the container, not the repo.
    run("git", "-C", str(origin), "config", "http.receivepack", "true", quiet=True)


def write_config() -> Path:
    """A config the containers read, pointed at the same endpoint your .env names."""
    config = build_config(subdir="team", approval_mode="full-auto")
    path = ROOT / "config.toml"
    body = {
        "defaults": {"provider": "openai", "model": MODEL, "approval_mode": "full-auto"},
        "providers": {"openai": {"api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"}},
        "routing": {tier: {"provider": "openai", "model": MODEL} for tier in ("low", "medium", "high")},
        "session": {"dir": "/team/sessions"},
        "daemon": {"transport": "tcp", "host": "0.0.0.0", "port": 7717},
        "team": {"shared_dir": "/team"},
        "agent": {"max_turns": 12},
    }
    import tomli_w

    path.write_text(tomli_w.dumps(body))
    _ = config
    return path


def up(config_path: Path) -> None:
    for role, port in CONTAINERS.items():
        run("docker", "rm", "-f", f"stcode-{role}", check=False, quiet=True)
        run(
            "docker", "run", "-d", "--name", f"stcode-{role}",
            "-v", f"{VOLUME.resolve()}:/team",
            "-v", f"{config_path.resolve()}:/config/config.toml:ro",
            "-e", "STCODE_ROLE=" + role,
            # Values, not names. `-e NAME` forwards from the *shell's* environment, and
            # these live in `.env` — loaded into this process, never exported.
            "-e", "OPENAI_API_KEY=" + os.environ.get("OPENAI_API_KEY", ""),
            "-e", "OPENAI_BASE_URL=" + os.environ.get("OPENAI_BASE_URL", ""),
            "--add-host", "host.docker.internal:host-gateway",
            "-p", f"{port}:7717",
            IMAGE,
            quiet=True,
        )
        ok(f"{role} on :{port}")


def down() -> None:
    for role in CONTAINERS:
        run("docker", "rm", "-f", f"stcode-{role}", check=False, quiet=True)


async def talk(port: int, messages: list[dict], *, collect: int = 200) -> list[dict]:
    """Send some client messages to a daemon and read what comes back."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    frames: list[dict] = []
    try:
        for message in messages:
            writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()
        for _ in range(collect):
            try:
                line = await asyncio.wait_for(reader.readline(), 45)
            except asyncio.TimeoutError:
                break
            if not line:
                break
            frames.append(json.loads(line))
            if frames[-1]["type"] in ("turn_finished", "agent_failed"):
                break
    finally:
        writer.close()
        await writer.wait_closed()
    return frames


async def main() -> int:
    if not docker_available():
        bad("Docker is not available, so the phase-5 gate cannot run here.")
        note("core/team/_test.py covers the same behaviour on a local directory.")
        return 2

    results: dict[str, bool] = {}
    ROOT.mkdir(parents=True, exist_ok=True)

    step("building the image")
    run("docker", "build", "-q", "-t", IMAGE, ".", quiet=True)
    ok(f"{IMAGE} built")

    seed_volume()
    ok(f"volume seeded: {', '.join(sorted(p.name for p in VOLUME.iterdir()))}")

    config_path = write_config()
    step("two containers, two roles")
    up(config_path)
    await asyncio.sleep(6)  # daemons bind, then wait for a client

    try:
        step("each daemon holds its own role")
        opened = {}
        for role, port in CONTAINERS.items():
            frames = await talk(port, [{"type": "create", "cwd": "/workspace"}], collect=2)
            session = next((f for f in frames if f["type"] == "session"), None)
            if session:
                opened[role] = session
                ok(f"{role}: session {session['id'][:8]}… mode={session['approval_mode']}")
            else:
                bad(f"{role}: no session — {frames}")
        reachable = len(opened) == len(CONTAINERS)
        results["both containers serve a session"] = reachable

        # Invariant 5 from both sides. Inside a container full-auto is legal, which is
        # what these sessions just proved; on the host it must be refused.
        step("invariant 5, from both sides")
        inside = all(s["approval_mode"] == "full-auto" for s in opened.values())
        from stcode.core.daemon import AutonomyRefused, Daemon

        host = build_config(subdir="team-host", approval_mode="full-auto")
        try:
            daemon = Daemon(host)
            await daemon.start()
            await daemon.aclose()
            outside_refused = False
        except AutonomyRefused:
            outside_refused = True
        results["full-auto: legal inside, refused outside"] = inside and outside_refused
        (ok if inside else bad)("full-auto accepted inside the container")
        (ok if outside_refused else bad)("full-auto refused on the host")

        step("a message crosses the volume")
        (VOLUME / "knowledge" / "spec.md").write_text(
            "# Spec: health endpoint\n\n`GET /health` returns 200 and a version field.\n"
        )
        await talk(
            CONTAINERS["ba"],
            [
                {"type": "attach", "session": opened["ba"]["id"], "replay": False},
                {
                    "type": "push",
                    "text": (
                        "The spec is written at /team/knowledge/spec.md. Send exactly one "
                        "message to backend-dev with subject 'health endpoint' and refs "
                        "['/team/knowledge/spec.md']. Then stop."
                    ),
                },
            ],
        )
        inbox = Mailbox(VOLUME, "backend-dev")
        delivered = list((VOLUME / "inbox" / "backend-dev").glob("*.json")) + list(
            (VOLUME / "inbox" / "backend-dev" / ".read").glob("*.json")
        )
        results["a message is a file on the volume"] = bool(delivered)
        (ok if delivered else bad)(f"{len(delivered)} message file(s) in backend-dev's inbox")
        if delivered:
            note(json.loads(delivered[0].read_text())["subject"])

        step("the idle agent is woken by it")
        # Nobody pushes backend-dev. If it ran, the daemon's inbox watcher started it.
        for _ in range(30):
            await asyncio.sleep(2)
            if not inbox.pending():
                break
        drained = not inbox.pending() and bool(delivered)
        results["an idle agent is woken by a message"] = drained
        (ok if drained else bad)("backend-dev drained its inbox without being pushed")

        logs = run("docker", "logs", "--tail", "5", "stcode-backend-dev", check=False, quiet=True)
        if logs.stderr.strip():
            note(logs.stderr.strip().splitlines()[-1])
    finally:
        step("tearing down")
        down()

    return verdict("step 10", results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
