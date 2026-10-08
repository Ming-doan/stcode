# 0006 — The UI and the engine are separate programs

**Status: accepted.** Decided 2026-10-05. Partly supersedes
[0004](0004-what-a-remote-client-may-change.md): a key may now be written, over the unix
socket only.

## Context

`stcode/cli` imported `stcode/core` freely. The TUI started the daemon *in its own
process*, read and wrote `config.toml` itself, and reconfigured the in-process daemon
directly when `/model` changed a key. Over a socket — `--daemonless` — the same actions
went through `get_config` / `set_config` instead. So every settings change had two code
paths, and the UI held three copies of the config to keep them apart: the file as
stored, the file with this run's flags folded in, and the daemon's copy in
`--daemonless`. `apply_cli_overrides`, `apply_provider_settings`, `_amend`, `_persist`
and `_push_config` existed to keep those three in step.

Separately, `core/` may be rewritten in another language. Every import from `cli/` into
`core/` is a reason that rewrite would have to start with the UI.

## The options

### A. Keep one process; tidy the config handling

Fewer copies, the same import graph. The two code paths remain, because a local daemon
is still an object the UI can call and a remote one is not.

### B. One program, two packages that never import each other

The UI reaches the engine only through its socket, and starts `stcode-daemon` as a
subprocess when none is listening. The daemon is the only reader and writer of
`config.toml`; the UI holds the daemon's last `config` frame and nothing else. Names
both sides need — approval modes, effort rungs — are copied; anything that changes when
a provider is added comes over the socket.

### C. Two distributions

Separate packages on PyPI, so a container image need not install `textual`. The same
boundary as B, plus release machinery that buys nothing until somebody needs the smaller
image.

## The decision

**B.** One configuration path instead of two: every client, local or remote, changes
settings with `set_config` and renders the reply. The import boundary is exactly the
wire format, which is the contract a rewrite of `core/` would have to keep anyway.

What that forced, and how it was answered:

- **The UI starting a daemon.** `cli/launcher.py` connects to the address it was given,
  and if nothing listens starts `stcode-daemon` there, logging to `~/.stcode/daemon.log`.
  It stops only a daemon it started — quitting is a detach. `--restart` sends `shutdown`
  first. The daemon's exit status distinguishes a rule 5 refusal (`2`) from a failure
  (`1`), and the launcher shows the log tail, so a refusal reads as a refusal.
- **First run.** The daemon scaffolds the config file; the UI opens the setup screen
  when the `config` frame says the default provider has no key.
- **Keys.** 0004 refused keys over the socket because a terminal on the wrong tab could
  repoint a fleet. That reasoning holds for TCP, and does not apply to a unix socket,
  which is chmod 0600 — its client is this user on this machine, the same person who
  could edit the file. So `api_key` (and `shutdown`) are accepted over unix and refused
  over TCP. Keys still never travel from the daemon to a client.
- **Flags.** `--model` was dropped: a per-run model is `/model` after start. `--mode`
  and `--role` become fields of `create`. `--team` applies only to `--headless`. The
  daemon's own flags are dotted config overrides, reapplied at every reload and never
  written.

## Consequences

- **The UI can no longer show anything before it connects.** Model and provider come
  from the daemon; the status line shows `—` until the first `config` frame.
- **A subprocess to manage.** A UI that crashes leaves its daemon running; the next
  `stcode` reuses it, which is the intended behaviour for a daemon anyway.
- **Copied names can drift.** `ApprovalMode` and the effort rungs exist twice. They
  change rarely, and a drift fails loudly — the daemon rejects an unknown value.
- **The settings screen is smaller.** Provider, key and model. Routing tiers, base URLs
  and concurrency caps are edited in the file, where the comments explaining them live.

## What would change this

A client with rights different from "this user on this machine" — a shared team daemon,
a web UI — needs identity on the connection, and then "unix socket = local user" stops
being the right test for what may be written. That is 0004's reopening condition too.
