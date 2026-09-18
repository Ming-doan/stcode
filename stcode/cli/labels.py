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

from stcode.core.harness.approvals import APPROVAL_MODES, ApprovalMode
from stcode.core.providers import PROVIDERS, ProviderConfig, default_model_for, key_env_for

Row = tuple[str, str]
"""One line of a card: a label and what it means. Every card is a list of these."""

# --------------------------------------------------------------------------- providers

PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI  (also any OpenAI-compatible endpoint)",
    "anthropic": "Anthropic",
    "google": "Google Gemini",
}

# OpenAI first: it's the default, and Select shows options in the order given.
PROVIDER_ORDER = ["openai", "anthropic", "google"]


def provider_options() -> list[tuple[str, str]]:
    """(label, value) pairs for the provider Select, preferred order first and any
    newly registered provider appended rather than silently dropped."""
    names = [p for p in PROVIDER_ORDER if p in PROVIDERS]
    names += [p for p in sorted(PROVIDERS) if p not in names]
    return [(PROVIDER_LABELS.get(name, name), name) for name in names]


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

EFFORT_ROWS: list[Row] = [
    ("none", "no thinking at all"),
    ("minimal", "the least the model offers"),
    ("low", "quick, for mechanical work"),
    ("medium", "the usual"),
    ("high", "for work that needs real reasoning"),
    ("xhigh", "more than high, where a provider has it"),
    ("max", "as much as the provider allows"),
]
"""Every rung any provider has. The narrower scales clamp — Anthropic has no `none`
rung below "off" and Gemini's tops out at high — which is the provider adapter's job
to say, not this list's job to hide."""

COMMANDS: list[Row] = [
    ("/model", "provider, key and model"),
    ("/effort", "how hard the model should think"),
    ("/mode", "approval mode"),
    ("/theme", "auto, dark or light"),
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


def commands_for(*, daemonless: bool) -> list[Row]:
    if daemonless:
        return list(COMMANDS)
    return [row for row in COMMANDS if row[0] not in DAEMONLESS_ONLY]


def daemonless_only(name: str) -> str:
    return f"/{name} needs --daemonless — this stcode is attached to the daemon it started."

CARD_HELP = "help"
CARD_COMMANDS = "commands"
CARD_FILES = "files"
CARD_MODE = "approval mode"
CARD_THEME = "theme"
CARD_EFFORT = "reasoning effort"
CARD_APPROVE = "approve?"
CARD_QUESTION = "the agent needs a decision"

SHORTCUT_ROWS: list[Row] = [
    ("?", "this card — backspace closes it"),
    ("/", "commands — type more to filter"),
    ("@", "files to mention — type more to filter"),
    ("enter", "send"),
    ("shift+enter", "newline (alt+enter too)"),
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
CARD_FOOTER_APPROVE = "y approve · n deny · esc denies"
CARD_FOOTER_QUESTION = "↑↓ then enter, or just type your own answer"

NO_FILES_HERE = "Nothing to mention — this workspace is on the daemon's machine."
NO_MCP_SERVERS = "No MCP servers. Add a .mcp.json to the workspace."
NO_SKILLS = "No skills found. See docs/guide/skills.md."


def file_rows(paths: list[str]) -> list[Row]:
    return [(path, "") for path in paths]


def mcp_rows(servers: list[dict[str, object]]) -> list[Row]:
    rows: list[Row] = []
    for server in servers:
        tools = server.get("tools") or []
        names = ", ".join(str(tool) for tool in tools) if isinstance(tools, list) else ""
        rows.append((str(server.get("name", "?")), names or "connected, no tools advertised"))
    return rows


def skill_rows(skills: list[dict[str, str]]) -> list[Row]:
    return [(skill.get("name", "?"), skill.get("description", "")) for skill in skills]


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
    return f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out{tail}"


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


# ------------------------------------------------------------------------- warnings

STATUS_NO_MODEL = "not set"
NO_MODEL_ERROR = "No model chosen. /model picks one."
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
SESSIONS_INTRO = "Only a parent session can be resumed — a sub-agent's is a transcript to read."
SESSIONS_RESUME = "Resume"


def session_line(row: dict[str, object]) -> str:
    """One session as a line: id, where, and the first thing that was said."""
    started = str(row.get("ts", ""))[:16].replace("T", " ")
    return f"{row.get('id', '?')}  {started}  {_tilde(row.get('cwd', ''))}"


def subsession_line(row: dict[str, object]) -> str:
    return f"  └ {row.get('agent_name', 'sub-agent')}"


def _tilde(path: object) -> str:
    """`~/w/stcode` rather than `/home/you/w/stcode`. The prefix is never the
    interesting part and it is always the longest."""
    text = str(path or "")
    home = os.path.expanduser("~")
    return f"~{text[len(home):]}" if home and text.startswith(home) else text


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


def approval_summary(tool: str, permission: str, arguments: dict[str, object]) -> str:
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
        body = ", ".join(f"{key}={_short(str(value))}" for key, value in arguments.items())
    return f"{tool} ({permission})\n{body}"


def _short(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------- settings screen

SETTINGS_TITLE_FIRST_RUN = "Welcome to stcode — one-time setup"
SETTINGS_TITLE = "Model settings"

SETTINGS_INTRO_FIRST_RUN = "Pick a provider and paste a key — skippable, /model changes it later."
SETTINGS_INTRO = "The key field is write-only; blank keeps the stored one."

FIELD_PROVIDER = "Provider"
FIELD_API_KEY = "API key"
FIELD_BASE_URL = "Base URL"
FIELD_MODEL = "Model"

BASE_URL_PLACEHOLDER = "optional — a proxy or compatible gateway"

BUTTON_SKIP = "Skip"
BUTTON_CANCEL = "Cancel"
BUTTON_SAVE = "Save"


def model_placeholder(provider: str) -> str:
    return f"optional — e.g. {default_model_for(provider)}"


def key_placeholder(provider: str, stored: ProviderConfig) -> str:
    if stored.api_key:
        return "•••••••• stored — type to replace"
    return f"paste your {provider_short_name(provider)} API key"


def key_hint(provider: str, stored: ProviderConfig) -> str:
    """Say which credential actually wins, since two can be in play at once."""
    env_name = stored.api_key_env or key_env_for(provider)
    if env_name and os.environ.get(env_name):
        return f"${env_name} is set and takes precedence over anything typed here."
    if stored.api_key:
        return "A key is stored in your config file (chmod 0600)."
    if env_name:
        return f"Or leave blank and export ${env_name} instead."
    return "Stored in your config file (chmod 0600)."
