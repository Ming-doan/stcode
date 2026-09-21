# 0004 — What a remote client may change

**Status: accepted.** Decided when `--daemonless` turned out to be showing the wrong
machine's settings.

## Context

`stcode --daemonless` is the shape where the agent is in a container and the terminal is
not. It built its config from `~/.stcode/config.toml` **on the terminal's machine** —
which describes a laptop that is not running anything. So the status line, the `/model`
screen and the routing tiers all reported settings that had no effect, and `/model`
wrote them to a file the daemon would never read.

The per-session half already worked: `set_meta` appends a `meta` record and the agent
re-reads its overrides before every call. What was missing is persistence. Restart the
container and the model is whatever `config.toml` said, so `/model` was a setting you
retyped every morning.

Reading the daemon's config is not the contested part — it is plainly a fact about the
daemon, like `info`. Writing it is.

## The options

### A. Read the daemon's config; keep `/model` per-session

Honest about where the settings live, and nothing new can be written over the socket.
But the original complaint stands: the choice does not survive a restart, and "the
config file in the container cannot be changed from the UI that edits configs" is a
missing feature, not a boundary.

### B. Read all, write everything

The settings screen edits the remote config wholesale — keys, `base_url`, routing tiers,
concurrency caps — and saves it. Most convenient, and symmetrical with the local shape.

The failure is not a privilege escalation; the socket is already the access control, and
anyone who can open it can run commands as the daemon. It is a *blast radius* problem
with no audit trail. A `/model` on the wrong tab repoints a daemon at a different
endpoint, and the person who did it has no reason to think they touched anything but a
model name. In a team that is N containers, and the values in question usually come from
environment variables rather than from the file at all — so writing the file would put a
value in a place that loses to `api_key_env` on the next start, silently.

### C. Read all, write `[defaults]`

`provider`, `model`, `reasoning_effort`, `approval_mode`. Everything else is shown,
redacted, and not accepted back.

## The decision

**C.**

The line is not "what is secret" — it is **what belongs to the conversation versus what
belongs to the deployment**. A model is a choice you make mid-task and change again ten
minutes later. A key, an endpoint, a concurrency cap and a routing tier are how the
operator provisioned this daemon, decided once at `docker run`.

So:

- `get_config` returns the whole config with every literal `api_key` replaced by `"***"`.
  `api_key_env` is left alone: the *name* of a variable is what diagnoses an
  unauthenticated daemon, and it is not itself the secret.
- `set_config` takes a patch and drops anything outside `CONFIGURABLE_DEFAULTS` rather
  than refusing it, because a client sending a whole config back is asking for the four
  keys it may change and should not have to know which those are.
- The write repoints routing tiers that were tracking the old default model, saves to the
  daemon's own path, and reconfigures the gateway in place — all three, or the change is
  a lie in one of three different ways.
- `guard_autonomy` runs on an `approval_mode` arriving this way. A config file is not a
  door around [rule 5](../architecture/daemon.md#full-auto-and-the-container-rule).

## Consequences

- **Two verbs that look alike.** `set_meta` reaches the running session; `set_config`
  survives the restart. The UI sends both for one `/model`, and
  [the protocol page](../sdk/daemon-protocol.md#reading-and-writing-the-daemons-config)
  has the table saying which is which.
- **The settings screen is half disabled in this shape.** Disabled, not hidden: "this
  daemon has no key set" is something you need to be able to read, and a field that
  silently would not save is worse than one that says so.
- **Changing a container's credentials still means redeploying it.** That is the
  intended cost. It is also where the credential came from.
- **`writable` is on the wire and mostly ignored.** A read-only `/config` mount is the
  ordinary container case, so the client greys the fields rather than offering an edit
  that fails.

## What would change this

If a daemon ever gets more than one client with different rights — a web UI, a shared
team daemon, anything with a notion of *who* is connected — then the boundary should be
per-client authorisation rather than a fixed key list, and this record is superseded.
That needs identity on the connection, which the protocol does not have and does not
currently need: today the socket is the credential, and it is one credential.

If the four keys turn out to be five — a `[session] dir` somebody wants to move
per-conversation, say — adding it is a one-line change to `CONFIGURABLE_DEFAULTS` **and
a paragraph here** explaining why it is a conversation-level fact. The list is short so
that growing it stays a decision.
