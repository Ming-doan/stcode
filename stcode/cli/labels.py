"""
Labels — every string the user reads, in one place.

Screens compose widgets; this module decides what those widgets say. Keeping the copy
together makes it possible to review tone in one pass, and keeps presentation-only
tables (mode colours, provider display names) out of `core/`, which shouldn't know a UI
exists.

Anything conditional is a function rather than a constant, so the *choice* of wording
lives here too instead of leaking back into the screens as an `if`.
"""

from __future__ import annotations

import os

from stcode.core.harness.approvals import APPROVAL_MODES, ApprovalMode
from stcode.core.providers import PROVIDERS, ProviderConfig, default_model_for, key_env_for

# --------------------------------------------------------------------------- wordmark

TAGLINE = "recursive language model coding agent"

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

# Rendered next to the mode name in the status bar; escalating warmth = escalating risk.
APPROVAL_MODE_COLOR: dict[ApprovalMode, str] = {
    "plan": "cyan",
    "suggest": "green",
    "auto-edit": "yellow",
    "full-auto": "red",
}


def mode_changed(mode: ApprovalMode) -> str:
    return f"Approval mode: {mode} — {APPROVAL_MODE_HELP[mode]}"


def unknown_mode(argument: str) -> str:
    return f"Unknown mode {argument!r}. One of: {', '.join(APPROVAL_MODES)}"


# ------------------------------------------------------------------------ chat screen

PROMPT_PLACEHOLDER = "Ask stcode…   /help for commands"

# role -> (gutter marker, rich style)
ROLE_PREFIX: dict[str, tuple[str, str]] = {
    "user": ("you", "bold cyan"),
    "assistant": ("stcode", "bold magenta"),
    # Reasoning is the model thinking out loud, not its answer, and must not read as
    # one — dimmed and labelled rather than shown in the assistant's voice.
    "thinking": ("···", "dim italic"),
    "notice": ("··", "dim"),
    "error": ("!!", "bold red"),
}

COMMAND_HELP = """\
/model          provider, API key, and model
/mode [name]    approval mode — no argument cycles
/connect        point this client at a different daemon
/clear          clear the transcript
/help           this list
/quit           exit
Anything else is sent to the model."""

STATUS_NO_MODEL = "⚠ not set — /model"
STATUS_NO_KEY = "⚠ no API key"

NO_MODEL_NOTICE = "No model selected — run /model to pick one."
NO_MODEL_ERROR = "No model selected. Run /model first."

SETUP_SKIPPED = (
    "Setup skipped — nothing written, so you'll be asked again next run. "
    "/model sets it up any time."
)

STREAM_STOPPED_SUFFIX = "  ⏹ stopped"
STREAM_STOPPED = "Stopped."
STREAM_EMPTY = "No response received."

# --------------------------------------------------------------------------- daemon

# The chat screen is a client now. These say so without making the user care: a daemon
# they did not start is worth one line, and one they did is worth none.

DAEMON_STARTING = "Starting a local agent daemon…"
CONNECTING = "Connecting…"

TOOL_OK = "✓"
TOOL_FAILED = "✗"


def daemon_connected(address: object, embedded: bool) -> str:
    if embedded:
        return f"Agent daemon started on {address}"
    return f"Attached to the agent daemon on {address}"


def session_started(session_id: str, cwd: object) -> str:
    return f"Session {session_id} in {cwd}"


def daemon_failed(exc: Exception) -> str:
    return f"Could not reach the agent daemon: {type(exc).__name__}: {exc}"


def turn_usage(usage: dict[str, object]) -> str:
    """Cache reads are shown because "turn 2 is cheaper" is otherwise unverifiable."""
    cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    tail = f" · {cached} cached" if cached else ""
    return f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out{tail}"


def tool_started(name: str, arguments: dict[str, object]) -> str:
    rendered = " ".join(f"{key}={_short(str(value))}" for key, value in arguments.items())
    return f"● {name} {_short(rendered, 110)}".rstrip()


def tool_finished(name: str, ok: bool, preview: str) -> str:
    mark = TOOL_OK if ok else TOOL_FAILED
    return f"  {mark} {name} — {_short(preview, 100) or '(no output)'}"


def supervisor_nudge(text: str) -> str:
    """A redirection, shown in full.

    Not shortened, unlike a tool preview: the agent is about to change direction and
    this is the only place the reason appears. A session that silently changes course
    is the debuggability problem §11 warns about.
    """
    return f"◆ supervisor — {' '.join(text.split())}"


def inbox_message(sender: str, subject: str, refs: list[str]) -> str:
    """A message from a teammate, as one line in the transcript."""
    tail = f" → {', '.join(refs)}" if refs else ""
    return f"✉ from {sender or 'unknown'}: {subject}{tail}"


def _short(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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

APPROVAL_TITLE = "Approve this?"
APPROVAL_YES = "Approve"
APPROVAL_NO = "Deny"
APPROVAL_DENIED = "Denied."
QUESTION_TITLE = "The agent needs a decision"
QUESTION_PLACEHOLDER = "Type your answer…"
QUESTION_SUBMIT = "Answer"


def approval_summary(tool: str, permission: str, arguments: dict[str, object]) -> str:
    """What is about to happen, in the terms the tool works in.

    The engine sends validated arguments rather than a sentence precisely so this can
    show a command as a command and a path as a path (`ApprovalRequest`'s docstring).
    """
    if tool in ("bash", "repl"):
        body = str(arguments.get("cmd") or arguments.get("code") or "")
    elif "path" in arguments:
        body = str(arguments["path"])
    else:
        body = ", ".join(f"{key}={_short(str(value))}" for key, value in arguments.items())
    return f"{tool} ({permission})\n\n{body}"


def config_location(path: object) -> str:
    return f"Config: {path}"


def config_saved(path: object) -> str:
    return f"Saved to {path}"


def no_key_error(provider: str) -> str:
    return f"No API key for {provider}. Run /model to add one."


def unknown_command(name: str) -> str:
    return f"Unknown command /{name} — /help for the list."


def stream_failed(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


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
