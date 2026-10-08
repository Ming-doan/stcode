# CLI and terminal UI

The CLI is a separate client program. It never imports `core/`: daemon operations
cross the JSONL socket, and only the daemon reads or writes `config.toml`.
See [decision 0006](../decisions/0006-cli-and-core-are-separate-programs.md) and
[decision 0007](../decisions/0007-cli-layers.md).

## Where code belongs

| Package | Responsibility | Dependencies |
| --- | --- | --- |
| `main.py`, `bootstrap.py` | Typer entry point and dependency construction | UI, logic, services |
| `ui/app.py` | Application lifecycle, themes, navigation, connection worker | UI and logic |
| `ui/screens/` | Pages and modal forms; chat interaction and presentation | Components and logic |
| `ui/components/` | Reusable widgets, transcript entries and Rich formatting | UI helpers, models and labels |
| `logic/` | Connection/session/settings workflows, commands, completion, request queue | Services and models |
| `services/` | Socket client, daemon launcher, preferences and daemon transport | Models and other services |
| `models.py` | Client vocabulary, launch options and preference values | Standard library and Pydantic |
| `labels.py` | User-facing copy and presentation tables | Models and pure command helpers |

Neither logic nor services import Textual, Rich, or UI modules. Components send
Textual messages to their containing screen; they do not reach into application
internals. Dependencies are passed through constructors, without a service locator.
The socket address value stays beside the transport in `services/client.py`; the
connect form uses that value without opening a socket itself.

## Ownership and lifetime

`StcodeApp` owns the connection worker. Showing settings or a session picker must not
stop streaming. `Connection` owns the current socket and the launcher; shutdown closes
the socket and stops only a daemon that launcher started. Reconnecting closes the
previous socket before opening another.

`ClientState` holds the latest daemon-reported configuration, effective session
settings, and information. Settings become visible after the daemon accepts them;
a refused configuration change must not modify the local snapshot or session.
Per-run mode and role flags remain session creation arguments, never config patches.

`ChatScreen` owns input, cards and interaction workers. Its presenter owns widget
handles for active streams and tools, keyed by agent and stream kind or call ID.
Streaming appends to an existing entry; it does not rebuild the transcript.
The status component owns its spinner timer. Approval and question requests remain
inline cards, ordered by arrival and correlated by execution ID.

Local preferences belong to `ui.toml`. `!` commands and `@` file discovery use the
attached session's workspace on the daemon, including remote connections. Commands
never enter agent history. Correlated workspace requests run independently of the
connection reader, so shell execution does not delay mode changes or approvals.
A refused full-auto mode change is a warning; the existing mode and connection stay active.

## Styles and tests

App and screen layouts use packaged `.tcss` files. Reusable component defaults live
in scoped `DEFAULT_CSS`; screens can override those defaults.

Tests mirror `logic/`, `services/` and `ui/` under `tests/cli/`. Import-boundary tests
protect the layers. Plain Python workflow tests cover refused settings, queue order
and connection cleanup. The existing real-socket TUI integration suite protects
keyboard behavior, trust, approvals, history, streaming and daemon workspace semantics.

Before delivery, run `uv run pytest`, the strict MkDocs build, and an installed-wheel
smoke test that loads the screens and their styles from outside the checkout.
