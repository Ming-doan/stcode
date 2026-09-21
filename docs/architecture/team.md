# Team mode

N containers, each one daemon and one agent with one role, sharing one volume.

```
/team                                   # mounted into EVERY container
├── knowledge/                          # shared truth; plain files
│   ├── architecture.md
│   ├── api-contract.md
│   └── decisions/2026-09-03-rate-limit.md
├── inbox/
│   ├── backend-dev/01M2…-from-ba.json
│   └── frontend-dev/
├── artifacts/                          # large output: reports, diffs, logs
└── repo.git                            # the bare origin every role clones
```

Team mode is **off unless `[team] role` is set**. Naming a role in a solo session only
selects a prompt; declaring one in config is a container saying a shared volume is
mounted.

## The bet: messaging has no protocol

Containers share a volume, so a message is a **file**. `send_message` writes JSON into
`/team/inbox/<role>/`, and the receiver drains its own directory. No registry, no routing
table, no service discovery, no N² socket mesh.

Delivery is a write then a rename, so a reader draining at the same moment sees a whole
message or nothing. Draining moves the file to `.read/` rather than deleting it — "who
told it that?" is a question you will ask.

```py
mailbox = Mailbox("/team", role="ba")
mailbox.send("backend-dev", "spec v2", refs=["/team/knowledge/spec.md"])
mailbox.drain()        # -> list[TeamMessage], moved to .read/
mailbox.pending()      # what the daemon's watcher polls
```

`core/team/mailbox.py` imports nothing from the harness, which keeps the arrow one-way:
the `send_message` *tool* lives in `core/team/tools.py` and closes over a mailbox — the
same shape `task` uses.

**`refs`, not content — enforced, not requested.** `send_message` refuses a body over
2000 characters, and refuses a ref that does not exist. That single rule is what keeps a
team's token cost from growing with the square of its size; a docstring asking nicely is
not enough, and a message pointing at nothing costs the recipient a whole turn to
discover.

## Who dispatches

**You do, per role.** No lead agent, no scheduler, no coordinator:

1. Attach to the **BA** container and describe the work.
2. BA writes the spec into `/team/knowledge/`, then `send_message`s the dev roles.
3. Switch to another container to watch, and push a message mid-run if it drifts.

"BA is where you start" is a *convention* in `ba.md` — one English sentence, not a class.
A `lead` role would be one more markdown file. That is why roles must stay data.

With `[team] wake_on_message`, the daemon polls the inbox and starts a turn when a
message lands **and the agent is idle**. Polling, not inotify: one `listdir` a second
costs nothing measurable, behaves the same on every filesystem a volume might be, and
needs no dependency.

## Knowledge needs no new tool

It is a directory; the file tools already work on it. Add `/team/knowledge` to the write
scope and state the convention in the role prompt. The thing everyone wants to build as a
"knowledge base service" is `mkdir`.

**The convention, enforced by prompt rather than code:** append-only *per file* — one
decision, one file at `decisions/<date>-<topic>.md`, never edit a file another role owns.
No locks, no CRDT, no merge.

## Roles

**One `config.toml` per agent** — prompt in `[agent] prompt`, settings in the sections
that were always there. Mounted at `/config/config.toml` it *is* the container's config,
which makes deploying N agents N mounts of one path and nothing to keep in step. On a
machine that holds several, they live in `~/.stcode/agents/` (or `.stcode/agents/`, or
`$STCODE_AGENTS_DIR`) and `--role` picks one by name, contributing its prompt only.
→ [agent profiles](harness.md#an-agent-profile-is-one-configtoml)

**Not in the package** — a role is a deployment fact, and a build that carried four of
them made adding a fifth a release. Copy a starting point from `examples/agents/`: `ba`,
`backend-dev`, `frontend-dev`, `devops`.

Each states what it owns, whose output it reads, and who it reports to. An unknown role
name refuses to start, which is what you want when a volume failed to mount.

**Team mode is off until it is switched on.** `[team] enabled`, or `STCODE_TEAM=1`;
naming a role is not enough. A role and a team are two facts — one says which agent this
is and is perfectly useful solo, the other says a shared volume is mounted — and merging
them meant a profile copied to a laptop started polling an inbox that did not exist.

A sub-agent (`task`) is still available inside a role container: a team splits the
product, `task` splits one role's work inside its own checkout. Sub-agents do **not** get
`send_message` — the discipline between roles stays with the roles.

## How work gets integrated

**A bare repository on the shared volume.** `/team/repo.git` is the origin. Each role
clones it into `/workspace`, works on its own branch, pushes, and **exactly one role
merges** — devops, by default.

Chosen over an SSH credential and a real remote for two reasons: no credential and no
network means the two-container gate runs on your machine as it stands, and it is still
real git — real branches, a real merge, a real merge boundary, which is the whole point
of *one agent = one role = one checkout = one merge boundary*.

Want pull requests instead? Point `[team] remote` at a URL and mount a key at
`[team] ssh_key`.

## Deployment contract

The repo ships a `Dockerfile` and this contract, nothing more. No `docker-compose.yml`,
no k8s manifests: how you bring up N containers is yours.

| | |
| --- | --- |
| Mounts | `/team` (shared volume), `/workspace` (where the agent clones), this agent's profile at `/config/config.toml` read-only |
| Env | `STCODE_CONFIG`, `STCODE_SANDBOX=1` (unlocks `full-auto`), `STCODE_TEAM=1` (turns team mode on), provider keys |
| Port | `[daemon] transport = "tcp"`, default 7717 |
| Role | `[team] role` in the mounted profile, or `--role` / `STCODE_ROLE` with `STCODE_AGENTS_DIR` |
| Clients | one at a time — `[daemon] max_clients` defaults to 1 in a container |
| Credentials | none. The origin is a bare repo on the volume |

## Failure modes to design against

| Failure | Cause | Guard |
| --- | --- | --- |
| Deadlock — two roles waiting on each other | nobody has a timeout | the supervisor's "lots of looking, no doing" heuristic |
| Message storm | no discipline | `refs` not content, enforced; the role prompt names who to report to |
| Divergent knowledge | no owner | each `knowledge/` file has exactly one owning role |
| Cost explosion | multi-agent ≈ 15× tokens | `[team] max_agents`; cheap tiers for support roles |
| Nobody integrates | no merge owner | exactly one role may merge |

## The honest caveat

Anthropic measured multi-agent at **~15× the tokens** of chat and single-agent at ~4×,
with token volume explaining **80% of performance variance** — much of what looks like
architectural cleverness is just paying more. They also list **"most coding tasks"** as a
*poor* fit.

Team mode pays off only when roles work on genuinely separable things (spec / UI / API /
pipeline). That is why the boundary is drawn at the service, not the file.

---

# The evaluation set

**Written, and not yet run.** Until it is, "does team mode help or merely spend 15× the
tokens" is an open question — and the paragraph above says it is a live one, not a
rhetorical one. Running this is the highest-value work left in the project.

The set is built to be able to say **no**.

## How to run one

1. Seed `/team/repo.git` with the task's starting state.
2. Run it **solo** first: one agent, one container, `--mode full-auto`. Record wall
   clock, total tokens, and the pass line.
3. Run it as a **team**: the roles listed, each its own container, started by attaching
   to `ba` and describing the task.
4. Compare. Team mode wins only if it passes where solo failed, or passes materially
   faster in wall clock. **Costing more tokens is expected and is not a win.**

Tokens come from the session files, which is the one place they are recorded:

```sh
jq -s 'map(select(.type=="usage")) | map(.input_tokens + .output_tokens) | add' \
   ~/.stcode/sessions/*.jsonl
```

## Reading the results

The set is deliberately split. **A–C should favour the team. D should not** — those tasks
are one indivisible change, and if team mode wins there, suspect the measurement before
believing the result. E is where it should visibly fail; a team that "wins" on E is a
team that did more work than the task needed.

| Group | Tasks | What it tests | Expected |
| --- | --- | ---: | --- |
| A | E01–E05 | Genuinely separable: spec, API, UI | team |
| B | E06–E10 | Contract-first work, one blocking dependency | team, narrowly |
| C | E11–E14 | Wide but shallow — many independent files | team |
| D | E15–E18 | One tangled change, heavy interdependence | **solo** |
| E | E19–E20 | Trivially small | **solo, by a lot** |

## Group A — genuinely separable

**E01 — Add a health endpoint and a status badge.** Roles: ba, backend-dev,
frontend-dev, devops. Seed: a small web app with an API and a UI, no health check.
*Pass:* `GET /health` returns 200 with a version field; the UI shows a badge driven by
it; `api-contract.md` describes the endpoint; one merge commit on `main`. *Watch:* does
frontend build against the contract, or sit idle waiting for the API?

**E02 — Rate limit the public API.** Roles: ba, backend-dev, frontend-dev, devops. Seed:
an API with no limiting; a UI that calls it in a loop. *Pass:* requests over the limit
get 429 with `Retry-After`; the UI backs off and shows a message; a test covers the 429
path; `decisions/<date>-rate-limit.md` records the chosen numbers. *Watch:* who picked
the limit? If backend picked it alone, the BA role is not working.

**E03 — Add pagination to a list endpoint and its table.** Roles: ba, backend-dev,
frontend-dev. *Pass:* cursor pagination in the API, contract updated first, the table
pages without a full reload, tests both sides.

**E04 — Internationalise the UI, translate the error catalogue.** Roles: ba, backend-dev,
frontend-dev. *Pass:* error codes come from the API, strings from the UI's catalogue;
adding a language touches no backend file.

**E05 — Add structured logging and a dashboard.** Roles: backend-dev, devops. *Pass:*
JSON logs with a request id; the dashboard config is in the repo; the id survives from
the edge to the log line.

## Group B — contract-first, with one real dependency

**E06 — Move authentication from sessions to tokens.** Roles: ba, backend-dev,
frontend-dev, devops. *Pass:* tokens issued and refreshed; the UI stores and sends them;
the old path is removed, not left dead; a migration note in `knowledge/decisions/`.
*Watch:* frontend must be blocked until the contract lands. Does it say so, or guess?

**E07 — Add a webhook the UI can subscribe to.** Roles: ba, backend-dev, frontend-dev.
*Pass:* signed webhook delivery, a replay endpoint, the UI updating without a poll.

**E08 — Version the API at /v2, keep /v1 working.** Roles: ba, backend-dev, devops.
*Pass:* both versions served and tested; the deprecation date is in a decision file.

**E09 — Add file upload with a size limit.** Roles: ba, backend-dev, frontend-dev,
devops. *Pass:* limit enforced server-side (not only in the UI); a clear error at the
limit; the limit appears once in the contract and is read from there.

**E10 — Add a background job and a progress endpoint.** Roles: backend-dev, frontend-dev,
devops. *Pass:* the job runs out of band, progress is pollable, the UI shows it, the
worker is in CI.

## Group C — wide and shallow

**E11 — Add type annotations across an untyped module tree.** Roles: backend-dev,
frontend-dev (split by directory). *Pass:* the type checker passes on both trees; no
`Any` added to silence it. *Watch:* if both roles touch one file, the split was wrong.

**E12 — Write missing tests for four independent services.** Roles: backend-dev,
frontend-dev, devops. *Pass:* coverage up on all four; no test asserts current-but-wrong
behaviour.

**E13 — Upgrade a dependency with breaking changes in three places.** Roles: ba,
backend-dev, frontend-dev, devops. *Pass:* the app builds and tests pass; the migration
is one decision file.

**E14 — Add CI for three services that have none.** Roles: devops, backend-dev. *Pass:*
three pipelines, each actually failing on a deliberately broken commit.

## Group D — one tangled change (solo should win)

**E15 — Fix a race in the connection pool.** backend-dev alone vs ba + backend-dev.
*Pass:* a failing reproduction first, then a fix that makes it pass. *Expect:* the team
run spends tokens on coordination for a single-file change.

**E16 — Refactor one 900-line module into four.** backend-dev alone vs two devs splitting
it. *Expect:* the split forces both agents into the same file — a design error, and this
task exists to demonstrate the cost of ignoring it.

**E17 — Track down a memory leak.** *Expect:* one agent holding the whole picture beats
two holding halves.

**E18 — Make a flaky test deterministic.** *Expect:* solo. Nothing to parallelise, and
the context is indivisible.

## Group E — small (solo should win by a lot)

**E19 — Fix a typo in an error message and its test.** *Expect:* solo, ~50× cheaper. If
the team run is close, the measurement is wrong.

**E20 — Bump a version number and its changelog entry.** *Expect:* the same. This is the
floor: the cost of team mode when there is no work.

## What to record

One row per run. The comparison, not the absolute number, is the finding.

| Task | Mode | Wall clock | Tokens | Passed | Notes |
| --- | --- | ---: | ---: | --- | --- |

Also worth recording, because they are the failure modes predicted above:

- **Deadlock** — two roles waiting on each other. Did the supervisor catch it?
- **Message storm** — count messages sent. Did any carry content instead of `refs`?
- **Divergent knowledge** — did two roles write the same `knowledge/` file?
- **Nobody integrated** — did anything reach `main`?
