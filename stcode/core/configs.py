"""
Config — locate, load, validate, save, and scaffold the stcode config.

Owns *how the file* is structured and what reads it: TOML, `.env`, the `[section]`
models. `GatewayConfig` is that on-disk shape, composing `ProviderConfig`/`RouteConfig`/
`RetryConfig` from `core/providers/gateway.py` rather than redefining them.

*Where* it lives is `core/common/paths.py`, because `core/harness/prompts/` looks for
role profiles in the same directory and cannot import this module without a cycle. Both
names are re-exported here, so every existing `from stcode.core.configs import
default_config_path` still works.

Credentials come from either side of `resolve_secret`: an env var named by `api_key_env`
(preferred — nothing secret touches disk), or a literal `api_key` from the setup screen.
The env var wins when set. Files written here are chmod 0600 because of that literal.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

# `default_config_path` is re-exported: `cli/` has always imported it from here, and
# it is still true that this module owns the config *file*. It no longer owns the
# *location* — `core/harness/prompts/` needs that too, and importing this module for it
# would be a cycle.
from stcode.core.common.paths import config_dir, config_exists, default_config_path
from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.providers import Difficulty, ProviderConfig, RetryConfig, RouteConfig, default_model_for
from stcode.core.providers.types import ReasoningEffort
from stcode.core.session import DEFAULT_SESSION_DIR
from stcode.core.team.mailbox import DEFAULT_TEAM_DIR

DEFAULT_SOCKET_PATH = "~/.stcode/daemon.sock"


class DefaultsConfig(BaseModel):
    """Session defaults the TUI shows and edits — what the user picked in `/model` and
    `/mode`, not routing policy, so the gateway ignores them.

    An empty `model` means "not chosen yet", which the chat screen warns about rather
    than silently guessing. An empty `reasoning_effort` means "whatever the model does
    on its own" — the rungs are not a scale every provider has, so there is no neutral
    middle to default to.
    """

    provider: str = "openai"
    model: str = ""
    reasoning_effort: ReasoningEffort | Literal[""] = ""
    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE


class AgentConfig(BaseModel):
    """Limits on one turn of the loop.

    `max_turns` is the tool-call ceiling *within* one turn, not a conversation length —
    the only thing between a model that keeps grepping and an unbounded bill. Hitting it
    ends the turn with a loud `AgentFailed`.

    `max_depth = 1` is not meant to be raised: sub-agents get neither `task` nor `repl`,
    and recursion with no budget is a fork bomb.

    `tools` / `exclude_tools` are the tool set this deployment wants — an allow-list
    replacing the default, and a subtraction from whatever is left. Applied after the
    agent is fully assembled, so `task`, `send_message` and MCP tools can be named too;
    a name that matches no tool refuses to start.
    """

    max_turns: int = 40
    max_depth: int = 1
    max_concurrent: int = 4
    enable_task: bool = True
    difficulty: Difficulty = "high"
    tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)


class SessionConfig(BaseModel):
    """Where transcripts live. `dir` takes `./.stcode/sessions` for per-project history."""

    dir: Path = Path(DEFAULT_SESSION_DIR)
    keep: int = 100


class DaemonConfig(BaseModel):
    """Where the daemon listens. Transport is configuration, not architecture.

    `unix` for solo — no port to collide with, and filesystem permissions are the access
    control. `tcp` for a container, which has no host filesystem to put a socket on. Same
    JSONL framing either way, so a third transport later is an adapter, not a protocol.
    """

    transport: Literal["unix", "tcp"] = "unix"
    socket: str = DEFAULT_SOCKET_PATH
    host: str = "127.0.0.1"
    port: int = 7717


class TeamConfig(BaseModel):
    """Team mode: one container, one agent, one role, one shared volume.

    Off unless `role` is set. `role` must match a markdown file in `.stcode/agents/`
    or `~/.stcode/agents/` (or `STCODE_AGENTS_DIR`) — a typo, or an unmounted volume,
    refuses to start rather than running an agent that owns nothing.

    The origin is a **bare repository on the shared volume**, `/team/repo.git`: no
    credentials, no network, every role clones and pushes branches, exactly one merges.
    For pull requests instead, point `remote` at a real URL — one line here and one
    sentence in the role prompts.
    """

    role: str = ""
    shared_dir: str = DEFAULT_TEAM_DIR
    max_agents: int = 6
    wake_on_message: bool = True
    """Whether the daemon starts a turn when a message arrives and the agent is idle.
    False means the agent only ever runs because you pushed it."""

    poll_interval: float = 1.0
    remote: str = "/team/repo.git"
    ssh_key: str = ""
    """Only needed if `remote` is a real URL. Mounted read-only; never copied."""


class SupervisorConfig(BaseModel):
    """The second pair of eyes on the trajectory.

    On by default and almost free: four counting heuristics cost nothing, and only a hit
    spends one `difficulty="low"` call.

    `every` counts tool-call iterations *within* a turn, not user messages — a task that
    loops does so inside one turn.
    """

    enabled: bool = True
    every: int = 8
    window: int = 30
    difficulty: Difficulty = "low"


class MCPConfig(BaseModel):
    """How MCP servers reach the agent.

    `code` writes each tool to `.stcode/mcp_servers/<server>/<tool>.py` and advertises
    nothing; the agent greps, reads the one file it needs, and calls it from `repl`.
    Three mid-sized servers cost 10-30k tokens *per turn* as definitions, a grep as code.

    `tools` advertises them the old way — the token argument is real but not universal.
    """

    expose: Literal["code", "tools"] = "code"
    enabled: bool = True


class TraceConfig(BaseModel):
    """Export the trajectory as OpenTelemetry spans, for an observability platform.

    Off by default, and the dependency is an extra (`uv sync --extra otel`): a coding
    session that is not being watched should not pay for a tracer, and enabling it
    without the extra installed warns rather than refusing to start.

    OTLP over HTTP, with `gen_ai.*` semantic conventions, so Langfuse, LangSmith,
    Phoenix or a plain Collector all read it — including the token counts, which those
    platforms turn into cost. Leave `endpoint` and `headers` empty to configure it the
    way every platform's own docs do, through `OTEL_EXPORTER_OTLP_ENDPOINT` and
    `OTEL_EXPORTER_OTLP_HEADERS`.

    `content` is the separate, louder switch: span *shapes* are not sensitive, prompts
    and file contents usually are.
    """

    enabled: bool = False
    endpoint: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    service_name: str = "stcode"
    content: bool = False


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routing: dict[Difficulty, RouteConfig] = Field(default_factory=dict)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    team: TeamConfig = Field(default_factory=TeamConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)


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
# max_concurrent = 1   # completions this endpoint serves at once; 0 = no cap. Set 1 for
#                      # a local model — parallel sub-agents otherwise queue against one
#                      # GPU and get dropped as 503s.

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

# Limits on one turn. max_turns caps tool calls within a turn, not conversation length.
# tools = [...] replaces the default tool set; exclude_tools = [...] subtracts from it.
# A name that matches no tool refuses to start.
[agent]
max_turns = 40
difficulty = "high"
# exclude_tools = ["web_search"]

# dir = "./.stcode/sessions" keeps transcripts with the project instead of in ~.
[session]
dir = "~/.stcode/sessions"
keep = 100

# Team mode. Empty role = solo; anything else must match ~/.stcode/agents/<role>.md
# (copy a starting point from examples/agents/).
# The origin is a bare repo on the shared volume — no credentials, no network.
# [team]
# role       = "backend-dev"
# shared_dir = "/team"
# remote     = "/team/repo.git"

# Watches for loops: same call 3x, most calls failing, no file written. Counting is
# free; only a hit costs one cheap model call. `every` is tool calls within a turn.
[supervisor]
enabled = true
every = 8

# MCP servers (from .mcp.json). "code" writes them to .stcode/mcp_servers/ and lets the
# agent import what it needs; "tools" advertises them in the prompt every turn.
[mcp]
expose = "code"

# OpenTelemetry export, for Langfuse / LangSmith / Phoenix / a Collector. Needs the
# `otel` extra: uv sync --extra otel. Leave endpoint and headers empty to use the
# standard OTEL_EXPORTER_OTLP_* environment variables instead.
# [trace]
# enabled  = true
# endpoint = "https://cloud.langfuse.com/api/public/otel/v1/traces"
# headers  = { Authorization = "Basic <base64 of public:secret>" }
# content  = false   # prompts and file contents stay out of spans unless this is true

# Where the daemon listens. `unix` on your own machine, `tcp` inside a container.
[daemon]
transport = "unix"
socket = "~/.stcode/daemon.sock"
# host = "0.0.0.0"   # when transport = "tcp"
# port = 7717
"""

SAVED_CONFIG_HEADER = """\
# stcode config — written by stcode. Hand-edits are preserved in value only: saving from
# the UI rewrites this file and drops any comments you add below.
#
# Credentials: `api_key_env` (an environment variable name) wins over a literal
# `api_key` whenever the variable is set. This file is chmod 0600 because it may hold
# the literal.
"""


def ensure_config_exists(path: Path | None = None) -> Path:
    """Scaffold a default config at `path` if none exists. Never overwrites."""
    path = path or default_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML)
        path.chmod(0o600)
    return path


def load_dotenv_files() -> None:
    """Load credentials from .env, most-specific first: `./.env`, then `~/.stcode/.env`.
    First value found wins, and real process env vars beat both."""
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(config_dir() / ".env", override=False)


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

    Unset fields are dropped rather than written as empty strings, so a config that
    never had a literal `api_key` does not grow one. Comments are not preserved.
    """
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass  # A shared/managed config dir we don't own is the user's business, not ours.

    # `mode="json"` because TOML has no Path and no enum: `dir` is a `Path` in memory
    # and has to reach `tomli_w` as the string it was read from.
    body = config.model_dump(mode="json", exclude_none=True)
    text = SAVED_CONFIG_HEADER + "\n" + tomli_w.dumps(body)

    # Write-then-rename so an interrupted save can't leave a truncated config behind.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def apply_cli_overrides(
    config: GatewayConfig,
    *,
    transport: str | None = None,
    socket: str | None = None,
    host: str | None = None,
    port: int | None = None,
    approval_mode: ApprovalMode | None = None,
    model: str | None = None,
    role: str | None = None,
) -> GatewayConfig:
    """Fold command-line overrides into a loaded config, for this run only.

    Never written back to disk: a flag is a decision about now, and `--mode full-auto`
    on one run must not leave `full-auto` in the file for the next. Which is why this
    returns a **copy** and leaves its argument alone — a UI that overwrites a config it
    had folded flags into writes those flags to disk, and a `--transport tcp` used once
    to reach a container becomes the transport the next bare `stcode` binds.

    Here rather than in `cli/` because both entry paths need it — the headless daemon
    and the UI — and the second must not have to import the first.
    """
    config = config.model_copy(deep=True)
    if transport is not None:
        config.daemon.transport = transport  # type: ignore[assignment]
    if socket:
        config.daemon.socket = socket
    if host:
        config.daemon.host = host
    if port is not None:
        config.daemon.port = port
    if approval_mode is not None:
        config.defaults.approval_mode = approval_mode
    if model:
        config.defaults.model = model
    if role is not None:
        # Setting a role turns team mode on, so `--role` is how one image serves every
        # role without a config file per container.
        config.team.role = role
    return config


def apply_provider_settings(
    config: GatewayConfig,
    *,
    provider: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    approval_mode: ApprovalMode | None = None,
    reasoning_effort: str | None = None,
    difficulty: Difficulty | None = None,
    routing_models: dict[str, str] | None = None,
    max_concurrent: int | None = None,
) -> GatewayConfig:
    """Fold one provider's settings into `config` and make it the session default.

    Returns a new config; the input is untouched. Blank strings mean "unset" — how the
    UI clears a base URL or a stored key.

    Tiers already on this provider, or still tracking the one being replaced, are
    repointed at the new model. A tier pinned to some third provider is left alone: that
    is a hand-edit the UI has no business undoing.

    `routing_models` names a model per difficulty tier and is applied **last**, on top
    of that repointing — it is what the setup screen's three routing fields write, and a
    tier the user typed into is a tier they meant. A blank entry means "follow the
    default model", which is what the repointing above already did.
    """
    updated = config.model_copy(deep=True)
    previous_provider = updated.defaults.provider

    existing = updated.providers.get(provider, ProviderConfig())
    updated.providers[provider] = ProviderConfig(
        api_key_env=(api_key_env or None) if api_key_env is not None else existing.api_key_env,
        api_key=(api_key or None) if api_key is not None else existing.api_key,
        base_url=(base_url or None) if base_url is not None else existing.base_url,
        base_url_env=existing.base_url_env,
        max_concurrent=existing.max_concurrent if max_concurrent is None else max_concurrent,
    )

    updated.defaults.provider = provider
    if model is not None:
        updated.defaults.model = model.strip()
    if approval_mode is not None:
        updated.defaults.approval_mode = approval_mode
    if reasoning_effort is not None:
        updated.defaults.reasoning_effort = reasoning_effort.strip()  # type: ignore[assignment]
    if difficulty is not None:
        updated.agent.difficulty = difficulty

    effective_model = updated.defaults.model or default_model_for(provider)
    if effective_model:
        for difficulty in ("low", "medium", "high"):
            route = updated.routing.get(difficulty)  # type: ignore[arg-type]
            if route is None:
                # No tier yet: point all three at the model the user chose. Splitting
                # cheap and expensive tiers is a deliberate edit.
                updated.routing[difficulty] = RouteConfig(  # type: ignore[index]
                    provider=provider, model=effective_model
                )
            elif route.provider in (provider, previous_provider):
                route.provider = provider
                route.model = effective_model

    # `tier`, not `difficulty`: that name is already this function's "which tier does the
    # main agent run at" parameter, and shadowing it here would be two unrelated meanings
    # for one identifier in one function.
    for tier, tier_model in (routing_models or {}).items():
        chosen = tier_model.strip()
        if not chosen:
            continue
        route = updated.routing.get(tier)  # type: ignore[call-overload]
        if route is None:
            updated.routing[tier] = RouteConfig(  # type: ignore[index]
                provider=provider, model=chosen
            )
        else:
            route.model = chosen

    return updated
