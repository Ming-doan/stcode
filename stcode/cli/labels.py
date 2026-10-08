"""
Labels — every string the user reads, in one place.

Screens compose widgets; this module decides what they say. Keeping the copy together
makes it reviewable in one pass, and keeps presentation-only tables (mode colours,
provider display names, the command list) out of `core/`, which should not know a UI
exists.

Anything conditional is a function rather than a constant, so the *choice* of wording
lives here too instead of leaking back into the screens as an `if`.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from stcode.cli.models import APPROVAL_MODES, ApprovalMode, REASONING_EFFORTS, Row
from stcode.cli.logic.commands import (
    next_approval_mode as next_approval_mode,
    parse_approval_mode as parse_approval_mode,
)

# --------------------------------------------------------------------------- providers

PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI  (also any OpenAI-compatible endpoint)",
    "anthropic": "Anthropic",
    "google": "Google Gemini",
}


def provider_options(config: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, value) pairs for the provider Select: the daemon's configured entries,
    then each library it could add an entry for."""
    providers: dict[str, Any] = config.get("providers", {})
    options = [
        (
            name
            if name == entry.get("provider")
            else f"{name}  ({entry.get('provider')})",
            name,
        )
        for name, entry in providers.items()
    ]
    options += [
        (f"{PROVIDER_LABELS.get(library, library)}  — new", library)
        for library in config.get("libraries", {})
        if library not in providers
    ]
    return options


def provider_short_name(provider: str) -> str:
    """The vendor name alone, for use mid-sentence."""
    return PROVIDER_LABELS.get(provider, provider).split()[0]


# ---------------------------------------------------------------------- approval modes

APPROVAL_MODE_HELP: dict[ApprovalMode, str] = {
    "plan": "read-only — no writes, no commands",
    "suggest": "ask before every write and command",
    "auto-edit": "auto-approve file edits, ask before commands",
    "full-auto": "approve everything — containerize before using",
}

# Rendered next to the mode name in the status line; escalating warmth = escalating risk.
APPROVAL_MODE_COLOR: dict[ApprovalMode, str] = {
    "plan": "cyan",
    "suggest": "green",
    "auto-edit": "yellow",
    "full-auto": "red",
}


def mode_rows() -> list[Row]:
    return [(mode, APPROVAL_MODE_HELP[mode]) for mode in APPROVAL_MODES]


def mode_changed(mode: ApprovalMode) -> str:
    return f"mode → {mode} — {APPROVAL_MODE_HELP[mode]}"


def unknown_mode(argument: str) -> str:
    return f"Unknown mode {argument!r}. One of: {', '.join(APPROVAL_MODES)}"


# ------------------------------------------------------------------------ the screen

PROMPT_PLACEHOLDER = "Ask stcode…    ? for help    / for commands    @ for files"

THEME_ROWS: list[Row] = [
    ("auto", "follow the terminal's own background"),
    ("dark", "always dark"),
    ("light", "always light"),
]

EFFORT_HELP: dict[str, str] = {
    "none": "no thinking at all",
    "minimal": "the least the model offers",
    "low": "quick, for mechanical work",
    "medium": "the usual",
    "high": "for work that needs real reasoning",
    "xhigh": "more than high, where a provider has it",
    "max": "as much as the provider allows",
}

EFFORT_CLAMPS: dict[str, dict[str, str]] = {
    "anthropic": {"minimal": "sent as low"},
    "google": {"xhigh": "sent as high", "max": "sent as high"},
}
"""Where a provider's own scale is narrower than the union.

Shown, not hidden: a rung that quietly does what the one below it does is worth one
parenthesis, and dropping it from the list would make the same card offer different
things on different days for reasons nobody could see. → `core/providers/types.py`
"""


def effort_rows(provider: str = "") -> list[Row]:
    """Every rung, with the clamp named for the provider currently in force.

    `provider` is the library (`openai`, …), which is what the clamps depend on.
    """
    clamps = EFFORT_CLAMPS.get(provider, {})
    rows: list[Row] = []
    for rung in REASONING_EFFORTS:
        detail = EFFORT_HELP.get(rung, "")
        clamp = clamps.get(rung)
        rows.append((rung, f"{detail} ({clamp})" if clamp else detail))
    return rows


EFFORT_ROWS: list[Row] = effort_rows()
"""The unqualified list, for callers with no provider in hand."""

COMMANDS: list[Row] = [
    ("/model", "provider, key, model and routing"),
    ("/effort", "how hard the model should think"),
    ("/mode", "approval mode"),
    ("/theme", "auto, dark or light"),
    ("/token", "what this session has spent"),
    ("/sessions", "pick up an earlier session"),
    ("/connect", "point this client at a different daemon"),
    ("/mcp", "the MCP servers this session connected to"),
    ("/skills", "the skills this session found"),
    ("/clear", "end this session and start a fresh one"),
    ("/help", "the help card"),
    ("/quit", "leave — the agent keeps working"),
]

DAEMONLESS_ONLY = {"/connect"}
"""Commands that only mean something when the agent is somewhere else. `stcode` on its
own started the daemon it is talking to, and offering to move the terminal off it would
leave that daemon running with nothing attached — which is a thing to do deliberately,
with `--daemonless`, not a thing to find in a list."""


def commands_for(*, daemonless: bool, skills: Sequence[str] = ()) -> list[Row]:
    """The `/` card: the built-in commands, then this session's skills.

    Skills are listed as commands because that is how they are reached — `/pdf` is one
    keystroke and one enter, where "please use the pdf skill" is a sentence to compose.
    They come last and are marked, so a skill can never be mistaken for something the
    UI itself does, and a skill named after a command cannot take it over.
    """
    rows = (
        list(COMMANDS)
        if daemonless
        else [row for row in COMMANDS if row[0] not in DAEMONLESS_ONLY]
    )
    taken = {row[0] for row in rows}
    return rows + [row for row in skill_commands(skills) if row[0] not in taken]


def skill_commands(skills: Sequence[str]) -> list[Row]:
    return [(skill_command(name), SKILL_COMMAND_HINT) for name in skills if name]


def skill_command(name: str) -> str:
    return f"/{name.strip().lstrip('/')}"


SKILL_COMMAND_HINT = "skill — loads its instructions, then does the work"


def daemonless_only(name: str) -> str:
    return f"/{name} needs --daemonless — this stcode is attached to the daemon it started."


CARD_HELP = "help"
CARD_COMMANDS = "commands"
CARD_FILES = "files"
CARD_MODE = "approval mode"
CARD_THEME = "theme"
CARD_EFFORT = "reasoning effort"
CARD_TOKENS = "tokens"
CARD_MCP = "mcp"
CARD_SKILLS = "skills"
CARD_APPROVE = "approve?"
CARD_QUESTION = "the agent needs a decision"

TOKEN_CARDS = (CARD_COMMANDS, CARD_FILES)
"""The only two cards the text under the cursor opens and closes.

Everything else — the pickers, the help, an approval — is opened by a command or by the
agent and keeps the floor until it is answered or dismissed. Without this distinction
the `Changed` message that `/effort` itself produces (submitting clears the input) finds
a card open with no token under the cursor, and closes the card that command just
opened. That is the whole "nothing happens when I press /effort" bug.
"""

SHORTCUT_ROWS: list[Row] = [
    ("?", "this card — backspace closes it"),
    ("/", "commands — type more to filter"),
    ("@", "files to mention — type more to filter"),
    ("!", "run a shell command on the daemon — never sent to the agent"),
    ("enter", "send"),
    ("ctrl+j", "newline (shift+enter and alt+enter where the terminal reports them)"),
    ("shift+tab", "change approval mode"),
    ("esc", "interrupt the turn, or close a card"),
    ("↑ ↓", "move through an open card"),
]


def help_rows(**paths: str) -> list[Row]:
    """The shortcuts, then where things are on disk.

    The paths are here rather than printed at startup because that is the trade: a
    config path nobody asked for is noise on every run, and a config path you cannot
    find when you need it is worse. `?` is where you ask.
    """
    rows = list(SHORTCUT_ROWS)
    rows += [(name, value) for name, value in paths.items() if value]
    return rows


CARD_FOOTER_CHOOSE = "↑↓ then enter · esc to close"
CARD_FOOTER_CLOSE = "backspace or esc to close"
CARD_FOOTER_APPROVE = "y approve · n deny · it stays until you answer"
"""No escape hatch listed, because there is not one. An approval card keeps the floor
until it is answered — dismissing it parked the turn on a reply with nowhere to come
from. → `cards.Card.dismissable`"""
CARD_FOOTER_QUESTION = "↑↓ then enter, or just type your own answer"

FILES_LOADING = "listing the workspace…"
NO_FILES_HERE = "Nothing to mention — this workspace is on the daemon's machine."
NO_MCP_SERVERS = "No MCP servers. Add a .mcp.json to the workspace."
NO_SKILLS = "No skills found. See docs/guide/skills.md."


def file_rows(paths: list[str]) -> list[Row]:
    return [(path, "") for path in paths]


def mcp_rows(servers: list[dict[str, object]]) -> list[Row]:
    rows: list[Row] = []
    for server in servers:
        tools = server.get("tools") or []
        names = (
            ", ".join(str(tool) for tool in tools) if isinstance(tools, list) else ""
        )
        rows.append(
            (str(server.get("name", "?")), names or "connected, no tools advertised")
        )
    return rows


def skill_rows(skills: list[dict[str, str]]) -> list[Row]:
    return [(skill.get("name", "?"), skill.get("description", "")) for skill in skills]


def skill_request(name: str, argument: str = "") -> str:
    """`/pdf split this in two` as the message the agent receives.

    A message, not a new protocol verb. The agent already has `skill(name)` and the
    catalogue that says what each one is for; what `/name` adds is saying *which* one
    without a sentence. Anything typed after the name is the task, and it is passed
    through untouched — it is the half only you can write.
    """
    task = argument.strip()
    opening = f'Use the "{name}" skill.'
    return f"{opening} {task}" if task else opening


# ----------------------------------------------------------------------------- tokens

NO_TOKENS_YET = "Nothing spent yet — this session has not called a model."

TOKEN_FOOTER = "Counted from this session's own usage records. esc to close."


def token_rows(totals: dict[str, int]) -> list[Row]:
    """What the session has spent, as a card.

    Input and output are the bill. The two cache lines are shown whenever they are
    non-zero because they are the *evidence* for prompt caching — "turn 2 is cheaper" is
    otherwise something you have to take on faith. `calls` is there because the
    interesting number is often per-call, and dividing is easier than counting boxes.
    """
    calls = totals.get("calls", 0)
    if not calls:
        return [(NO_TOKENS_YET, "")]
    written = totals.get("cache_creation_input_tokens", 0)
    read = totals.get("cache_read_input_tokens", 0)
    rows: list[Row] = [
        ("input", f"{totals.get('input_tokens', 0):,}"),
        ("output", f"{totals.get('output_tokens', 0):,}"),
    ]
    if written:
        rows.append(("cache write", f"{written:,}"))
    if read:
        rows.append(("cache read", f"{read:,} — charged at a fraction of input"))
    rows.append(
        ("total", f"{totals.get('input_tokens', 0) + totals.get('output_tokens', 0):,}")
    )
    rows.append(("model calls", f"{calls:,}"))
    return rows


# ------------------------------------------------------------------------------ shell

SHELL_PREFIX = "!"

SHELL_RUNNING = "running…"


def shell_timed_out(seconds: float) -> str:
    return (
        f"timed out after {seconds:g}s. Raise `shell_timeout` in ui.toml (max 120), or "
        "ask the agent to run it so it can wait."
    )


def shell_failed(exc: Exception) -> str:
    return f"could not run it: {type(exc).__name__}: {exc}"


SHELL_NO_COMMAND = "! on its own does nothing — type a command after it."


# --------------------------------------------------------------------- platform notes
#
# Everything the platform did, as one dim rule across the screen. Short, because a rule
# that wraps is two rules.


def session_started(cwd: object) -> str:
    return f"session in {_tilde(cwd)}"


def session_resumed(session_id: str) -> str:
    return f"resumed {session_id}"


def session_cleared() -> str:
    return "new session — the last one is in /sessions"


def daemon_connected(address: object, embedded: bool) -> str:
    return f"daemon started on {address}" if embedded else f"attached to {address}"


def model_changed(model: str) -> str:
    return f"model → {model}"


def effort_changed(effort: str) -> str:
    return f"effort → {effort}"


def theme_changed(theme: str) -> str:
    return f"theme → {theme}"


def turn_usage(usage: dict[str, object]) -> str:
    """Cache reads are shown because "turn 2 is cheaper" is otherwise unverifiable."""
    cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    tail = f" · {cached} cached" if cached else ""
    return (
        f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out{tail}"
    )


def supervisor_nudge(text: str) -> str:
    """A redirection, shown in full.

    Not shortened, unlike a tool preview: the agent is about to change direction and
    this is the only place the reason appears. A session that silently changes course
    is the debuggability problem the docs warn about.
    """
    return f"◆ supervisor — {' '.join(text.split())}"


def inbox_message(sender: str, subject: str, refs: list[str]) -> str:
    """A message from a teammate, as one line in the transcript."""
    tail = f" → {', '.join(refs)}" if refs else ""
    return f"✉ from {sender or 'unknown'}: {subject}{tail}"


STREAM_STOPPED = "stopped"

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
"""The braille spinner in the status line while a turn is in flight.

A spinner rather than a word, because the thing it answers is "did my message land?" —
and a static "working" cannot tell you whether the screen is alive. It goes in the
status line, directly under the input, rather than into the transcript: the transcript
is the record of what happened, and a row that appears and disappears is not that.
"""

STATUS_WORKING = "working"


def working_status(frame: str) -> str:
    return f"{frame} {STATUS_WORKING}   "


# ------------------------------------------------------------------------- warnings

STATUS_NO_MODEL = "—"
SETUP_SKIPPED = "Setup skipped — nothing written. /model sets it up any time."


def no_key_error(provider: str) -> str:
    return f"No API key for {provider}. /model adds one."


def daemon_failed(exc: Exception) -> str:
    return f"Could not reach the agent daemon: {type(exc).__name__}: {exc}"


def unknown_command(name: str) -> str:
    return f"Unknown command /{name} — ? for the list."


def not_connected() -> str:
    return "Not connected to a daemon yet."


APPROVAL_DENIED = "denied"

# --------------------------------------------------------------------------- trust

TRUST_TITLE = "trust this folder?"
TRUST_BODY = (
    "The agent will read, write and run commands here with your privileges.\n"
    "Cancel leaves — there is nothing it can usefully do in a folder you have not "
    "approved."
)
TRUST_YES = "Trust"
TRUST_NO = "Cancel"

# ------------------------------------------------------------------------- sessions

SESSIONS_TITLE = "sessions"
SESSIONS_EMPTY = "No sessions yet. A session file appears when you say something."
SESSIONS_INTRO = (
    "Only a parent session can be resumed — a sub-agent's is a transcript to read."
)
SESSIONS_RESUME = "Resume"


def session_line(row: dict[str, object]) -> str:
    """One session as a line: id, where, and the first thing that was said."""
    started = str(row.get("ts", ""))[:16].replace("T", " ")
    return f"{row.get('id', '?')}  {started}  {_tilde(row.get('cwd', ''))}"


def subsession_line(row: dict[str, object]) -> str:
    """A sub-agent's row: its name, then its id.

    The id is here because the name is not unique — two turns can both spawn an
    `api-scout` — and the id is what you need in order to open the transcript or grep
    the sessions directory for it.
    """
    return f"  └ {row.get('agent_name') or 'sub-agent'}  {row.get('id', '')}"


def _tilde(path: object) -> str:
    """`~/w/stcode` rather than `/home/you/w/stcode`. The prefix is never the
    interesting part and it is always the longest."""
    text = str(path or "")
    home = os.path.expanduser("~")
    return f"~{text[len(home) :]}" if home and text.startswith(home) else text


# --------------------------------------------------------------------------- connect

CONNECT_TITLE = "Connect to an agent daemon"
CONNECT_INTRO = "Where is the daemon? A container usually exposes TCP on 7717."
CONNECT_RETRY = "Nothing is listening there. Check the address, or start a daemon."

FIELD_TRANSPORT = "Transport"
FIELD_SOCKET = "Socket path"
FIELD_HOST = "Host"
FIELD_PORT = "Port"

HOST_PLACEHOLDER = "the container's address, e.g. 10.0.0.4"
BUTTON_CONNECT = "Connect"

TRANSPORT_LABELS: dict[str, str] = {
    "unix": "unix socket  (this machine)",
    "tcp": "tcp  (a container, or another host)",
}


def transport_options() -> list[tuple[str, str]]:
    return [(TRANSPORT_LABELS[name], name) for name in ("unix", "tcp")]


def bad_port(value: str) -> str:
    return f"{value!r} is not a port number."


def no_daemon_here(address: object) -> str:
    """`--daemonless` says never start one, so a missing daemon is a question."""
    return f"No daemon is listening on {address}."


DAEMONLESS_CANCELLED = "Not connected. /connect to try another address."

# ------------------------------------------------------------------------- approvals


APPROVAL_BODY_LINES = 10
"""How many lines of what you are approving the card shows.

Bounded because the card is laid out top to bottom — title, body, the two answers,
the footer — and a body that grows without limit pushes **deny** and **approve** off
the bottom of the screen. A forty-line `repl` cell did exactly that, leaving a card
that asks a question and shows no way to answer it.

Ten because the head and the tail of a cell are where its intent is: the imports and
the first statement say what it is going to do, the last line says what it leaves
behind. The middle is where a long cell is most repetitive.
"""


def approval_summary(
    tool: str,
    permission: str,
    arguments: dict[str, object],
    *,
    lines: int = APPROVAL_BODY_LINES,
) -> str:
    """What is about to happen, in the terms the tool works in.

    The engine sends validated arguments rather than a sentence precisely so this can
    show a command as a command and a path as a path (`ApprovalRequest`'s docstring).
    """
    if tool in ("bash", "repl"):
        # `command` is `bash`'s parameter and `code` is `repl`'s. Getting this wrong is
        # invisible in review and fatal in use: the card renders with an empty body and
        # asks you to approve running nothing in particular.
        body = str(arguments.get("command") or arguments.get("code") or "")
    elif "path" in arguments:
        body = str(arguments["path"])
    else:
        body = ", ".join(
            f"{key}={_short(str(value))}" for key, value in arguments.items()
        )
    return f"{tool} ({permission})\n{_elide_lines(body, lines)}"


def _elide_lines(text: str, limit: int) -> str:
    """Keep the head and the tail, and say how much is missing.

    The same discipline as a tool result (invariant 1): elided, never summarised, and
    the count is in the marker so "is that the whole cell?" has an answer on the card.
    """
    rows = text.splitlines()
    if len(rows) <= limit:
        return text
    head = limit - 3
    hidden = len(rows) - head - 2
    return "\n".join([*rows[:head], f"… {hidden} more lines …", *rows[-2:]])


def _short(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------- settings screen

SETTINGS_TITLE_FIRST_RUN = "Welcome to stcode — one-time setup"
SETTINGS_TITLE = "Model settings"

SETTINGS_INTRO_FIRST_RUN = (
    "Pick a provider and paste a key — skippable, /model changes it later."
)
SETTINGS_INTRO = "Saved to the daemon's config. The key field is write-only; blank keeps the stored one."

FIELD_PROVIDER = "Provider"
FIELD_API_KEY = "API key"
FIELD_MODEL = "Model"

BUTTON_SKIP = "Skip"
BUTTON_CANCEL = "Cancel"
BUTTON_SAVE = "Save"


def model_placeholder(default_model: str) -> str:
    return f"blank — {default_model}" if default_model else "the model to use"


def key_placeholder(has_key: bool) -> str:
    return "•••••••• set — type to replace" if has_key else "paste your API key"


def key_hint(key_env: str, *, has_key: bool, editable: bool) -> str:
    """Say where the key comes from, and why the field may be locked."""
    if not editable:
        return "Keys can only be changed from the daemon's own machine (unix socket)."
    if has_key:
        return "A key resolves for this provider. Typing one stores it in the config file (0600)."
    if key_env:
        return f"Or leave blank and export ${key_env} for the daemon."
    return "Stored in the daemon's config file (0600)."
