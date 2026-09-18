# 0003 — What the TUI is allowed to own

**Status: accepted.** Decided while rebuilding the chat screen, because three separate
features turned out to be the same question.

## Context

The TUI acquired three pieces of state that are not the agent's:

1. a **theme** — dark or light, following the terminal;
2. a list of **trusted folders**, so the agent is not turned loose in a directory the
   user has not looked at;
3. `/clear`, which users expect to wipe the conversation.

Each one has an obvious cheap answer, and all three of the cheap answers put a fact
about one person's terminal somewhere it does not belong.

The constraints already in force:

- `~/.stcode/config.toml` is read by the **daemon**, and the daemon is what a container
  runs (`--headless`). `[daemon]`, `[providers]`, `[routing]`, `[agent]` are all things a
  container legitimately needs.
- [Hard rule 4](../architecture/session.md#append-only-and-that-is-load-bearing):
  sessions are append-only JSONL. No rewriting, no truncation, no fork.

## The options

### A. Everything in `config.toml`

One file, one loader, one `save_config`. `[ui] theme = "auto"`, `[ui] trusted = [...]`.

Cheapest by a wide margin — the plumbing exists and is tested.

But `config.toml` is the file a container reads, and a container has no theme. Worse,
`trusted` is a security-relevant list keyed to *this machine's* paths, and it would now
ship in the same document as `[team] remote` and the provider keys, to be copied around
and mounted. Trust that travels by `docker cp` is not trust.

### B. A separate `~/.stcode/ui.toml`

A second small file, a second reader, ~40 lines of it. `config.toml` stays the agent's;
`ui.toml` stays this terminal's.

Costs a file, and costs answering "which of the two is it in?" — which is why `?` shows
both paths.

### C. Somewhere else entirely — `$XDG_STATE_HOME`, a JSON blob, a keyring

Correct by the letter of the XDG spec. Also a fourth location for stcode's files, when
the project has spent some effort getting it down to one directory, and the spec's own
`$XDG_CONFIG_HOME` fallback lands in `~/.config` rather than `~/.stcode` anyway.

## The decision

**B**, plus two consequences that follow from it rather than being separate choices.

### `~/.stcode/ui.toml` holds the theme and the trusted list

```toml
theme = "auto"                       # auto | dark | light
trusted = ["/home/you/work/stcode"]
```

Flat, not `[ui]`-sectioned: the file *is* the UI's, so a section header would be the
same word twice. It is written by the TUI and read by nothing else — `core/` does not
import it, and a headless daemon never opens it.

It lives next to whichever `config.toml` is in force, so pointing `STCODE_CONFIG` at a
scratch directory takes the preferences with it. It is **not** moved by `--config`: that
flag chooses an agent configuration for one run, and a run should not be able to forget
which folders you trust.

### `/clear` starts a new session; it does not clear anything

Rule 4 says the file is not rewritten, so "clear the history in the current file" is not
available at any price. Of what is left:

- **truncate anyway** — breaks the rule, and the supervisor and the trace exporter both
  read that file;
- **delete the file** — keeps the rule (nothing is rewritten) but destroys a trajectory
  the user may want tomorrow;
- **start a new session** — keeps the rule and keeps the transcript.

The third. The old session stays on disk and is one `/sessions` away.

This is only unsurprising because of the next part.

### A session file is not created until its first message

`Session.create(defer=True)` mints the id and holds the `meta` record in memory; the
file appears on the first `append`. Which is what makes `/clear` feel like clearing: mash
it five times and there are no five empty files, because there were never any files.

It also fixes something that was already wrong. Every `stcode` that was started and
closed without a word — checking a flag, hitting the wrong directory — left a session
file that `stcode sessions` then listed forever.

## Consequences

- **Two files to explain.** Mitigated, not solved: `?` shows both paths and the current
  session file, so neither has to be memorised.
- **`ui.toml` is not portable, deliberately.** Copy it to another machine and the
  trusted paths are for directories that may not exist there. That is the right failure:
  it asks again.
- **A session id can exist with no file behind it.** `Session.started` is the test. The
  daemon drops an unstarted session with no watchers on detach, so a client that
  connects, looks around and leaves holds nothing open.
- **`Session.list()` no longer shows every session that was ever opened** — only the ones
  that were used. Someone debugging "where did my session go" will land here.
- **The trust prompt is a wall, not a warning.** Cancel exits the program. An agent
  whose entire purpose is running commands in the current directory has nothing to offer
  a user who declines, and a third "read-only for now" state would be `--mode plan`
  wearing a disguise.

## What would change this

If a second client ever needs the theme — a web UI for the daemon, say — then `ui.toml`
is the wrong home for it and a `ui` section in the *session* or a per-client preference
service is the right one. The trusted list would not move with it: that is about the
machine the agent runs on, and it would stay beside the daemon.

If the deferred session file turns out to hide a real failure — an agent that dies before
its first append and now leaves no evidence it ran at all — the fix is to write the file
on `create` and delete it on close-if-empty, not to go back to eager creation.
