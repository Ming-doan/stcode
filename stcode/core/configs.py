"""
Config — locate, load, validate, save, and (if missing) scaffold the stcode config.

Owns everything about *where* config and credentials live and *how the file on disk*
is structured — paths, TOML, `.env` loading. `GatewayConfig` here is that on-disk
shape; it composes `ProviderConfig`/`RouteConfig`/`RetryConfig`, which are the LLM
Gateway's own domain vocabulary and live in `core/providers/gateway.py` — this module
doesn't redefine them, only assembles and (de)serializes them.

Credentials can come from either side of `resolve_secret`: an environment variable
named by `api_key_env` (preferred — nothing secret touches disk), or a literal
`api_key` written into the config file by the first-run setup screen. The env var wins
whenever it's actually set. Files this module writes are chmod 0600 precisely because
that literal may be in them.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.providers import Difficulty, ProviderConfig, RetryConfig, RouteConfig, default_model_for


class DefaultsConfig(BaseModel):
    """Session defaults the TUI shows and edits. The gateway ignores these — they're
    what the user picked in `/model` and `/mode`, not routing policy.

    An empty `model` means "not chosen yet", which the chat screen surfaces as a warning
    rather than silently guessing.
    """

    provider: str = "openai"
    model: str = ""
    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routing: dict[Difficulty, RouteConfig] = Field(default_factory=dict)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)


DEFAULT_CONFIG_TOML = """\
# stcode config. Prefer keeping API keys in the environment: set `api_key_env` to the
# name of a variable and put the actual secret in your shell or a .env file (see
# stcode/core/configs.py:load_dotenv_files for where those are picked up from). A
# literal `api_key` is also honoured — the setup screen writes one there, and the file
# is chmod 0600 for that reason.

# What the TUI starts with. An empty model means "not chosen yet" — /model sets it.
[defaults]
provider = "anthropic"
model = ""
approval_mode = "suggest"

[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"
# base_url = "https://api.anthropic.com"  # override for a proxy/self-hosted gateway

[providers.openai]
api_key_env = "OPENAI_API_KEY"

[providers.google]
api_key_env = "GEMINI_API_KEY"

# Each tier picks a provider + model. api_key_env/base_url are optional per tier —
# omit them and the tier falls back to that provider's main config above.
[routing.low]
provider = "anthropic"
model = "claude-haiku-4-5"

[routing.medium]
provider = "anthropic"
model = "claude-sonnet-5"

[routing.high]
provider = "anthropic"
model = "claude-opus-5"

[retry]
max_attempts = 3
base_delay = 1.0
max_delay = 20.0
jitter = true
"""

SAVED_CONFIG_HEADER = """\
# stcode config — written by stcode. Hand-edits are preserved in value only: saving from
# the UI rewrites this file and drops any comments you add below.
#
# Credentials: `api_key_env` (an environment variable name) wins over a literal
# `api_key` whenever the variable is set. This file is chmod 0600 because it may hold
# the literal.
"""


def default_config_path() -> Path:
    """~/.stcode/config.toml (or %APPDATA%\\stcode\\config.toml on Windows);
    override with the STCODE_CONFIG env var."""
    env_path = os.environ.get("STCODE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "stcode" / "config.toml"
    return Path.home() / ".stcode" / "config.toml"


def config_exists(path: Path | None = None) -> bool:
    """Whether a config file is already present — the whole first-run test. Startup asks
    for a provider and key only when this is False."""
    return (path or default_config_path()).exists()


def ensure_config_exists(path: Path | None = None) -> Path:
    """Scaffold a default config file at `path` (or the default location) if none exists
    yet. Returns the path either way. Never overwrites an existing file."""
    path = path or default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML)
        path.chmod(0o600)
    return path


def load_dotenv_files() -> None:
    """Load credentials from .env files, most-specific first: ./.env (project-local —
    useful when a checkout needs its own keys) then ~/.stcode/.env (a user-global
    default, mirroring config.toml's location). First value found for a given key wins;
    real process env vars always take precedence over both.
    """
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(default_config_path().parent / ".env", override=False)


def load_config(path: Path | None = None, *, create_if_missing: bool = False) -> GatewayConfig:
    path = path or default_config_path()
    if create_if_missing:
        path = ensure_config_exists(path)

    load_dotenv_files()

    if not path.exists():
        raise FileNotFoundError(
            f"No stcode config found at {path}. Set STCODE_CONFIG, create it yourself "
            "(see stcode.example.toml), or call load_config(create_if_missing=True)."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)
    return GatewayConfig.model_validate(data)


def save_config(config: GatewayConfig, path: Path | None = None) -> Path:
    """Write `config` back out as TOML, atomically and mode 0600.

    Unset (None) fields are dropped rather than written as empty strings, so a config
    that never had a literal `api_key` doesn't grow one. Comments in the previous file
    are not preserved — the header explains that to whoever opens it next.
    """
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass  # A shared/managed config dir we don't own is the user's business, not ours.

    body = config.model_dump(mode="python", exclude_none=True)
    text = SAVED_CONFIG_HEADER + "\n" + tomli_w.dumps(body)

    # Write-then-rename so an interrupted save can't leave a truncated config behind.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def apply_provider_settings(
    config: GatewayConfig,
    *,
    provider: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    approval_mode: ApprovalMode | None = None,
) -> GatewayConfig:
    """Fold one provider's settings into `config` and make it the session default.

    Returns a new config; the input is left alone. Blank strings mean "unset" — that's
    how the UI clears a base URL or a stored key.

    Difficulty tiers follow the default: a tier already on this provider, or one still
    tracking the provider this is replacing, is repointed at the newly chosen model.
    A tier deliberately pinned to some third provider is left alone — that's a hand-edit
    the UI has no business undoing.
    """
    updated = config.model_copy(deep=True)
    previous_provider = updated.defaults.provider

    existing = updated.providers.get(provider, ProviderConfig())
    updated.providers[provider] = ProviderConfig(
        api_key_env=(api_key_env or None) if api_key_env is not None else existing.api_key_env,
        api_key=(api_key or None) if api_key is not None else existing.api_key,
        base_url=(base_url or None) if base_url is not None else existing.base_url,
        base_url_env=existing.base_url_env,
    )

    updated.defaults.provider = provider
    if model is not None:
        updated.defaults.model = model.strip()
    if approval_mode is not None:
        updated.defaults.approval_mode = approval_mode

    effective_model = updated.defaults.model or default_model_for(provider)
    if effective_model:
        for difficulty in ("low", "medium", "high"):
            route = updated.routing.get(difficulty)  # type: ignore[arg-type]
            if route is None:
                # No tier configured yet: point all three at the one model the user has
                # actually chosen. Splitting cheap/expensive tiers is a deliberate edit.
                updated.routing[difficulty] = RouteConfig(  # type: ignore[index]
                    provider=provider, model=effective_model
                )
            elif route.provider in (provider, previous_provider):
                route.provider = provider
                route.model = effective_model

    return updated
