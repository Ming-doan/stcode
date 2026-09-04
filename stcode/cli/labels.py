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
    "notice": ("··", "dim"),
    "error": ("!!", "bold red"),
}

COMMAND_HELP = """\
/model          provider, API key, and model
/mode [name]    approval mode — no argument cycles
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
